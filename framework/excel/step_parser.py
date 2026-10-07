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
    proceed, hover, submit       -> click        (target = quoted text, else rest of line; "hover X and
                                                    click 'Y'" resolves Y, since hovering to reveal an
                                                    overlay button is a precondition for the click, not a
                                                    separately verifiable action)
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
    "hover": "click",
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


@dataclass
class SubStep:
    step_no: int
    action: str  # one of _VERB_MAP's values, or "unparseable"
    target: str
    raw_line: str


def parse_test_steps(test_step_cell: str) -> list[SubStep]:
    """Split the multi-line 'Test Step' cell into ordered SubStep entries."""
    lines = [ln for ln in test_step_cell.splitlines() if ln.strip()]
    sub_steps: list[SubStep] = []
    for i, line in enumerate(lines, start=1):
        m = _LINE_RE.match(line)
        body = m.group(1) if m else line.strip()
        sub_steps.append(_parse_line(i, body))
    return sub_steps


def _parse_line(step_no: int, body: str) -> SubStep:
    first_word_match = re.match(r"^([A-Za-z]+)\b", body)
    if not first_word_match:
        return SubStep(step_no, "unparseable", body, body)

    verb = first_word_match.group(1).lower()
    action = _VERB_MAP.get(verb)
    if action is None:
        if _PRECONDITION_RE.match(body) and not _QUOTED_RE.search(body):
            return SubStep(
                step_no, "unparseable",
                "describes a cross-test-case session precondition, not a resolvable "
                "UI action; this per-row executor does not carry session state across "
                "test cases",
                body,
            )
        return SubStep(step_no, "unparseable", body, body)

    rest = body[first_word_match.end():].strip()

    if action == "noop":
        return SubStep(step_no, "noop", rest or body, body)

    if action == "navigate":
        url_match = _URL_RE.search(body)
        target = url_match.group(0) if url_match else rest
        return SubStep(step_no, "navigate", target, body)

    # Connector ("under"/"in") wins over a quoted phrase when there is real text
    # before it: "Enter name and email under 'New User Signup!'" means fill the
    # *name and email* fields -- the quoted text names the surrounding section,
    # not the field itself. A quoted phrase only becomes the target when there
    # is no connector, or nothing meaningful precedes it (e.g. "Click 'Login'").
    connector_match = re.search(r"\b(under|in)\b", rest, re.IGNORECASE)
    pre_connector = rest[: connector_match.start()].strip() if connector_match else ""

    if connector_match and pre_connector:
        target = pre_connector
    else:
        quoted = _QUOTED_RE.search(rest)
        target = quoted.group(1) if quoted else rest

    if not target:
        return SubStep(step_no, "unparseable", body, body)

    return SubStep(step_no, action, target, body)
