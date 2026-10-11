"""Mini-grammar for the free-text 'Test Step' cell.

Each numbered line is parsed into a SubStep(action, target, raw_line). The
grammar is intentionally small and documented (see README "Step Parsing
Grammar"); any line that does not match a known verb is NEVER guessed — it
becomes a SubStep with action="unparseable" so the executor can mark that one
sub-step Blocked with a clear reason, per spec "refuse to guess."

Grammar (per numbered line, case-insensitive on the verb):
    "<n>. <Verb> ['<Target>'] [rest of line as extra detail]"

Verb vocabulary -> action:
    launch, open                 -> noop        (no element to resolve, e.g. "launch browser")
    navigate, go                 -> navigate     (target = URL token, else rest of line)
    click, press, tap, add,
    proceed, submit               -> click        (target = quoted text, else rest of line)
    hover                         -> hover         (target = quoted text, else rest of line; a real pointer
                                                    move onto the target, revealing any hover-only overlay
                                                    UI underneath it, e.g. a product card's "View Product"
                                                    link. "Hover X and click 'Y'" splits into two sub-steps,
                                                    hover X then click Y, the same way "X and go to Y" does --
                                                    X must stay genuinely hoverable, not just a precondition
                                                    folded into Y's own resolution: a target with NO
                                                    always-visible copy of its own (confirmed live: a
                                                    product's "View Product" link, unlike its "Add to cart"
                                                    button, only exists inside a hover-revealed overlay) is
                                                    otherwise structurally unreachable no matter how clearly
                                                    it's described, since the DOM walker only ever indexes
                                                    elements already visible when it runs.
    enter, fill, input, type,
    change                       -> type         (target = text before "under"/"in" if present, else quoted text, else rest of line)
    select, choose, pick         -> select        (target = quoted text, else rest of line)
    check, tick                  -> check         (target = quoted text, else rest of line)
    upload, attach               -> upload        (target = quoted text, else rest of line)
    observe, verify, confirm,
    locate                       -> assert_visible (target = rest of line; checked against Expected Result, never guessed)
    accept                       -> accept_dialog  (browser alert; target = rest of line)
    leave                        -> noop          (e.g. "Leave Email and Password empty" -- deliberately
                                                    not typing anything is the correct behaviour, not a
                                                    skipped step)
    scroll                       -> assert_visible (scrolling itself is not independently verifiable without
                                                    a specific element target; treated as an informational
                                                    checkpoint, same as observe/verify)
A line whose only recognizable verb is "login"/"logout" with no further
quoted target (e.g. "Login with valid credentials", "Login") describes a
cross-test-case session precondition, not an atomic UI action this per-row
executor can perform -- it is reported "unparseable" with that reason spelled
out explicitly, rather than silently treated as done.
Any other leading verb, or a line with no recognizable verb, -> "unparseable".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$")
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_URL_RE = re.compile(r"https?://\S+")

_VERB_MAP: dict[str, str] = {
    "launch": "noop",
    "open": "noop",
    "navigate": "navigate",
    "go": "navigate",
    "click": "click",
    "press": "click",
    "tap": "click",
    "add": "click",
    "proceed": "click",
    "hover": "hover",
    "submit": "click",
    "enter": "type",
    "fill": "type",
    "input": "type",
    "type": "type",
    "change": "type",
    "select": "select",
    "choose": "select",
    "pick": "select",
    "check": "check",
    "tick": "check",
    "upload": "upload",
    "attach": "upload",
    "observe": "assert_visible",
    "verify": "assert_visible",
    "confirm": "assert_visible",
    "locate": "assert_visible",
    "scroll": "assert_visible",
    "accept": "accept_dialog",
    "leave": "noop",
}

_PRECONDITION_RE = re.compile(r"(?i)^(login|logout|log in|log out)\b")
_TRY_LOGIN_AGAIN_RE = re.compile(r"(?i)\btry\b.*\blog(?:ging)?\s+in\b.*\bagain\b")
_REPEAT_WITH_VALUE_RE = re.compile(r"(?i)^repeat\b.*['\"]([^'\"]+)['\"]")


@dataclass
class SubStep:
    step_no: int
    action: str  # one of _VERB_MAP's values, or "unparseable"
    target: str
    raw_line: str
    context: str | None = None
    # The generic content-type word (e.g. "email") that named *what* to type,
    # when "Enter email in Subscription field" rewrote `target` to the
    # surrounding field name ("Subscription field") for element-resolution
    # purposes and would otherwise lose it. Kept separate from `context`
    # (which still serves resolution_description unchanged) so a later
    # Test Data value lookup can still know the field wants an email, even
    # when no Test Data label is literally named "email".
    value_hint: str | None = None
    # True when the line ends in "...and accept the browser alert" (or
    # similar) -- a native JS alert/confirm/prompt that must be explicitly
    # accepted or every subsequent WebDriver command raises
    # UnexpectedAlertPresentException. Generic to any app whose action
    # triggers a native dialog, not a wording specific to one site.
    dialog_after: bool = False
    # A literal value to type, taken directly from the step text itself
    # (action == "repeat_with_value"), overriding the usual Test Data lookup
    # -- see _REPEAT_WITH_VALUE_RE.
    literal_value: str | None = None


_AND_GO_TO_RE = re.compile(r"(?i)\s+and\s+go\s+to\s+(.+)$")
# "Enter comment, click 'Place Order'" and "Hover first product and click
# 'Add to cart'" are both two actions on one numbered line, just like the
# "and go to" case above -- a trailing ", <verb> '<target>'" OR
# " and <verb> '<target>'" clause names a second, separate action to perform
# afterward. Built from _VERB_MAP's own keys so it recognizes exactly the
# same verb vocabulary as everywhere else in this grammar, not a hand-picked
# subset. The quoted target is required specifically so this never
# misfires on an unrelated " and <word>..." with no quote at all (e.g.
# "Enter username and password", a multi-field target handled separately).
_COMMA_CLAUSE_RE = re.compile(
    r"(?i)(?:,|\s+and)\s*(" + "|".join(_VERB_MAP.keys()) + r")\s+(['\"][^'\"]+['\"].*)$"
)


def parse_test_steps(test_step_cell: str) -> list[SubStep]:
    """Split the multi-line 'Test Step' cell into ordered SubStep entries."""
    lines = [ln for ln in test_step_cell.splitlines() if ln.strip()]
    sub_steps: list[SubStep] = []
    for i, line in enumerate(lines, start=1):
        m = _LINE_RE.match(line)
        body = m.group(1) if m else line.strip()
        # Detect compound-line patterns against the body with any leading
        # "On <page>, " clause already stripped -- otherwise that clause's
        # own comma ("On home page, click 'Women' in sidebar") is mistaken
        # for the compound-line separator this block is actually looking
        # for. The stripped prefix is preserved and re-added to the first
        # half below so its raw_line/section-targeting is unaffected.
        page_clause_match = _LEADING_PAGE_CLAUSE_RE.match(body)
        prefix = page_clause_match.group(0) if page_clause_match else ""
        detect_body = body[len(prefix):]

        # "Add product to cart and go to Cart" is two actions on one numbered
        # line, not one: a trailing "and go to <place>" clause names a second,
        # separate destination to reach afterward -- generic manual-test-case
        # phrasing, not specific to any one app or action verb. Splitting it
        # into its own synthetic "Go to <place>" line and parsing that exactly
        # like any standalone line of that shape keeps the two code paths
        # identical rather than duplicating the navigate-target logic here.
        and_go_match = _AND_GO_TO_RE.search(detect_body)
        if and_go_match and not _TRY_LOGIN_AGAIN_RE.search(detect_body):
            main_body = prefix + detect_body[: and_go_match.start()].strip()
            second_body = f"Go to {and_go_match.group(1).strip()}"
            sub_steps.append(_parse_line(i, main_body))
            sub_steps.append(_parse_line(i, second_body))
            continue
        # "Enter comment, click 'Place Order'" -- same compound-line shape,
        # a different connector ("," + a second verb instead of "and go to").
        # Without this split, the comma-separated clause (with its own quoted
        # target) was swallowing the WHOLE line as a single step's target,
        # losing "comment" (what to actually type) entirely.
        comma_match = _COMMA_CLAUSE_RE.search(detect_body)
        if comma_match:
            main_body = prefix + detect_body[: comma_match.start()].strip()
            second_body = f"{comma_match.group(1)} {comma_match.group(2)}".strip()
            main_sub = _parse_line(i, main_body)
            # "Hover first product and click 'Add to cart'" -- the FIRST
            # clause names which product ("first product", or a specific
            # name), but the split above hands the second clause only its
            # own bare verb+target ("click 'Add to cart'"), with nowhere
            # for that to go. Confirmed live on automationexercise.com:
            # without it, "Add to cart" is identical across every product
            # on the page, and the AI correctly refuses to guess which one
            # -- the step was never actually resolvable, not flaky.
            # Carrying the first clause's own target forward as "for
            # <target>" feeds the SAME "for"-connector + Test-Data-ordinal
            # lookup already relied on for "...for the first product" steps
            # elsewhere, rather than a second, parallel mechanism.
            if (
                re.search(r"(?i)\bproduct\b", main_sub.target)
                and not re.search(r"(?i)\b(under|in|for)\b", second_body)
            ):
                second_body = f"{second_body} for {main_sub.target}"
            sub_steps.append(main_sub)
            sub_steps.append(_parse_line(i, second_body))
            continue
        sub_steps.append(_parse_line(i, body))
    return sub_steps


_LEADING_PAGE_CLAUSE_RE = re.compile(r"(?i)^on\s+[\w/ ]+?\s+page,?\s*")
_TRAILING_DIALOG_CLAUSE_RE = re.compile(
    r"(?i)\s*,?\s*and\s+accept\s+(the\s+)?(browser\s+)?(alert|confirm(ation)?|prompt|popup|dialog)\s*$"
)


def _parse_line(step_no: int, raw_line: str) -> SubStep:
    # "On <page>, <verb> ..." names which page the step happens on before the
    # actual action -- a common, generic way to write a step, not specific to
    # any one application. Strip it so the real verb (e.g. "click") is what
    # gets matched, rather than the leading "On" defeating verb detection.
    # raw_line is kept as the original, unstripped text for logs/reports.
    body = _LEADING_PAGE_CLAUSE_RE.sub("", raw_line)
    dialog_after = bool(_TRAILING_DIALOG_CLAUSE_RE.search(body))
    body = _TRAILING_DIALOG_CLAUSE_RE.sub("", body)
    first_word_match = re.match(r"^([A-Za-z]+)\b", body)
    if not first_word_match:
        return SubStep(step_no, "unparseable", body, raw_line)

    verb = first_word_match.group(1).lower()
    action = _VERB_MAP.get(verb)
    if action is None:
        # "Try logging in again (with the same/old credentials)" is NOT the
        # same thing as a bare "Login" precondition: the point of the step
        # IS the (expected-to-fail) login attempt itself, verified by the
        # row's own Expected Result text (e.g. "...fails with 'incorrect'"),
        # not by whether the login succeeded. Mechanically perform the app's
        # login_steps without treating rejection as a row failure -- generic
        # "try X again" phrasing, not tied to one app's wording.
        if _TRY_LOGIN_AGAIN_RE.search(body):
            return SubStep(step_no, "attempt_login", body, raw_line)
        repeat_match = _REPEAT_WITH_VALUE_RE.search(body)
        if repeat_match:
            # "Repeat with invalid email 'abc'" means: re-run the most
            # recent data-entry (type) step -- and whatever click followed
            # it -- using this literal value instead of a Test Data lookup.
            # Generic "repeat with a different value" phrasing, common in any
            # suite's negative/boundary-value test rows, not app-specific.
            return SubStep(step_no, "repeat_with_value", body, raw_line, literal_value=repeat_match.group(1))
        precondition_match = _PRECONDITION_RE.match(body)
        if precondition_match and not _QUOTED_RE.search(body):
            matched_verb = precondition_match.group(1).lower()
            if matched_verb in ("login", "log in"):
                # A bare "Login" / "Login with valid credentials" names a
                # real, performable action -- not just a precondition to
                # wave through -- if a working test account is available
                # (cached from an earlier successful registration this run,
                # or configured via env vars). The executor resolves it
                # using the app's declarative login_steps; it is reported
                # Blocked, not silently skipped, if no account is available.
                return SubStep(step_no, "auto_login", body, raw_line)
            return SubStep(
                step_no, "unparseable",
                "describes a cross-test-case session precondition, not a resolvable "
                "UI action; this per-row executor does not carry session state across "
                "test cases",
                raw_line,
            )
        return SubStep(step_no, "unparseable", body, raw_line)

    rest = body[first_word_match.end():].strip()

    # "open" is ambiguous: "Open browser" is a true no-op (Selenium already
    # launched it), but "Open product detail page" names a real destination
    # that must actually be reached -- treating every "open" as a no-op would
    # silently skip navigating anywhere and leave later steps acting on
    # whatever page the browser happened to already be on.
    if verb == "open" and rest and rest.lower() not in ("the browser", "browser"):
        action = "click"

    if action == "noop":
        return SubStep(step_no, "noop", rest or body, raw_line)

    if action == "navigate":
        url_match = _URL_RE.search(body)
        target = url_match.group(0) if url_match else rest
        return SubStep(step_no, "navigate", target, raw_line)

    # Connector ("under"/"in"/"for") wins over a quoted phrase when there is
    # real text before it: "Enter name and email under 'New User Signup!'"
    # means fill the *name and email* fields -- the quoted text names the
    # surrounding section, not the field itself. A quoted phrase only
    # becomes the target when there is no connector, or nothing meaningful
    # precedes it (e.g. "Click 'Login'"). "for" names which item a
    # same-labelled control belongs to on a listing page -- "Click 'Add to
    # cart' for the first product" must keep "the first product" as
    # resolution context, or every product's identically-labelled button
    # becomes indistinguishable (confirmed live: without this, "first
    # product" and "second product" steps resolved to the SAME generic
    # "Add to cart" description and could land on the wrong, or an
    # ambiguous, button).
    # Searched against a masked copy of `rest`, not `rest` itself: a quoted
    # label can legitimately contain "in"/"for"/"under" as one of its own
    # words (e.g. "the 'Sign In' button", "'Built for you'") -- matching
    # against the raw string would treat that as the connector and split
    # the quoted phrase itself in half. The mask keeps every character's
    # position identical (so connector_match.start()/.end() below still
    # index correctly into the real `rest`), it just blanks out anything
    # inside quotes first so a connector word can only match outside one.
    quote_masked_rest = _QUOTED_RE.sub(lambda m: "#" * len(m.group(0)), rest)
    connector_match = re.search(r"\b(under|in|for)\b", quote_masked_rest, re.IGNORECASE)
    pre_connector = rest[: connector_match.start()].strip() if connector_match else ""

    context = None
    value_hint = None
    if connector_match and pre_connector:
        quoted_context = _QUOTED_RE.search(rest[connector_match.end():])
        context = quoted_context.group(1).strip() if quoted_context else rest[connector_match.end():].strip()
        # "Enter keyword in search box" names the field after the connector;
        # "Enter name and email under 'Signup'" names fields before it. Keep
        # the natural-language section/context separately for the resolver.
        value_hint_match = re.search(r"\b(keyword|value|text|email)\b", pre_connector, re.I)
        if action == "type" and not quoted_context and value_hint_match:
            target = context
            context = None
            value_hint = value_hint_match.group(1).lower()
        else:
            # "Click 'Logout' in the menu" -- pre_connector is itself a quoted
            # phrase here, same as the no-connector branch below handles; without
            # unwrapping it, the target would keep its literal quote marks.
            pre_connector_quoted = _QUOTED_RE.fullmatch(pre_connector)
            target = pre_connector_quoted.group(1) if pre_connector_quoted else pre_connector
    else:
        quoted_all = _QUOTED_RE.findall(rest)
        if action == "click" and len(quoted_all) > 1:
            # "Click 'Products' > 'View Product'" is breadcrumb-style shorthand
            # for two sequential clicks (click Products, which reveals/leads
            # to View Product, then click that) -- not one element matching
            # the whole phrase. Generic test-authoring shorthand, not tied to
            # any one app's wording; the executor splits on " > " to perform
            # each click in order.
            target = " > ".join(q.strip() for q in quoted_all)
        else:
            quoted = _QUOTED_RE.search(rest)
            target = quoted.group(1) if quoted else rest

    if not target:
        return SubStep(step_no, "unparseable", body, raw_line)

    return SubStep(step_no, action, target, raw_line, context, value_hint, dialog_after)
