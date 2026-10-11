"""Ties reader -> resolver -> actions -> writer together for one test case row.

Aggregation rule (documented assumption, since the workbook has one row per
test case holding several numbered sub-steps, not one action per row):
    - Row Status = the worst outcome among its sub-steps: Fail > Blocked > Pass.
    - Once a sub-step is Blocked or Fail, remaining sub-steps in that row are
      skipped (same "stop on failure within a row" policy as the spec's
      row-level on_failure:stop, applied at sub-step granularity).
    - Element Source = comma-separated distinct tiers actually used by the row's
      sub-steps (e.g. "Cache,AI"). Tokens Used = sum across sub-steps.
    - Expected Result clauses that the mini-grammar could not turn into a
      deterministic check ("unverifiable") are reported in Actual Result but
      never silently treated as a pass or fail.

Test Data -> sub-step value matching (documented assumption): for a "type"
sub-step, the value is picked from a "Label: value" line in Test Data whose
label shares a word with the sub-step's target description; if none matches,
the sub-step is Blocked (never guesses a value), except when Test Data has
exactly one line, which is used directly.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from framework.browser import SeleniumPage as Page

from selenium.common.exceptions import UnexpectedAlertPresentException

from framework.actions import verbs
from framework.actions.ad_utils import clear_overlays_before_interaction
from framework.config.allowlist import AllowListGuard
from framework.excel.expected_parser import parse_expected_result
from framework.excel.reader import TestCaseRow
from framework.excel.step_parser import SubStep, parse_test_steps
from framework.llm.client import LlmClient
from framework.llm.usage_logger import UsageLogger
from framework.reporting.console import ConsoleNarrator
from framework.resolver.resolve import resolve_element
from framework.store.account_cache import load_account, save_account
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue
from framework.synonyms import FIELD_CONCEPT_SYNONYMS

_STATUS_RANK = {"Pass": 0, "Skipped": 0, "Blocked": 1, "Fail": 2}


@dataclass
class RowExecutionResult:
    test_case_id: str
    status: str
    actual_result: str
    element_source: str
    tokens_used: int
    llm_model: str | None
    execution_time_ms: int
    executed_at: str


_HOME_REFERENCE_RE = re.compile(r"(?i)^\s*(to\s+)?(the\s+)?(home\s*page|homepage|the\s+site|home)\s*\.?\s*$")


def resolve_navigate_target(step_target: str, base_url: str) -> str | None:
    """An explicit URL in the step text is used as-is. "Home page"/"the
    site" (and the like) have no URL to extract but unambiguously mean the
    app's base URL. Anything else ("Go to Cart", "Navigate to Checkout",
    "Go to the Profile page") names a real, specific destination that is
    *not* the same as home -- previously this fell through to base_url
    regardless, which is wrong for literally every such step except the ones
    that genuinely mean home. Returning None for that case tells the caller
    to resolve and click whatever link leads there instead, the same way a
    'click' step would -- reusing the existing resolver pipeline rather than
    needing a registry of page-name-to-URL mappings that could never
    generalize across arbitrary applications anyway.
    """
    if re.match(r"^https?://", step_target):
        return step_target
    if not step_target.strip() or _HOME_REFERENCE_RE.match(step_target):
        return base_url
    return None


_FIELD_SPLIT_RE = re.compile(r",|\band\b", re.IGNORECASE)


def split_multi_field_targets(target: str) -> list[str]:
    """Split a multi-field target ('Title, Password, DOB, ...' or 'name and new
    email', 'registered email and correct password') into atomic per-field
    targets. By the time `target` reaches here, the step parser has already
    stripped the leading verb ('Fill'/'Enter'), so this checks for the
    comma/"and" shape itself, not a verb prefix.

    Regression: a real run showed "name and new email" and "registered email
    and correct password" treated as ONE target (no comma present), which
    resolved a single element and typed one value into it -- sometimes the
    wrong value, since a target naming both "email" and "password" can match
    either Test Data label depending on which line the matcher reaches first.
    Splitting on "and" as well as "," prevents one combined description from
    ever being resolved as a single element when it actually names several
    distinct fields.
    """
    if not _FIELD_SPLIT_RE.search(target):
        return [target]
    return [f.strip() for f in _FIELD_SPLIT_RE.split(target) if f.strip()]


def _normalise_type_target(target: str) -> str:
    """Remove value instructions that are not part of an element description."""
    cleaned = re.sub(r"\s+to\s+[-+]?\d+(?:\.\d+)?\s*$", "", target, flags=re.I)
    cleaned = re.sub(r"^(?:an?\s+)?invalid\s+", "", cleaned, flags=re.I)
    # A leading "to " is filler left over from phrasings like "Go to Cart" /
    # "Navigate to Checkout" once the verb itself is stripped -- not part of
    # the destination's own name, for any such step on any app.
    cleaned = re.sub(r"^to\s+", "", cleaned, flags=re.I)
    return cleaned.strip() or target


def _extract_typed_value(
    target: str, test_data: str, *, run_id: str | None = None
) -> tuple[str | None, str]:
    lines = [ln.strip() for ln in test_data.splitlines() if ln.strip()]
    if len(lines) == 1 and ":" not in lines[0]:
        return lines[0], "used sole Test Data line"
    target_words = _normalised_field_words(target)
    # The same generic concept synonyms the verifier uses for element
    # plausibility (e.g. "keyword" <-> "search") apply here too: a Test Data
    # label naming the field by its real-world name ("Search: xyz") should
    # still satisfy a step that describes it by generic task wording
    # ("keyword"), on any app, not just when the words match verbatim.
    expanded_target_words = set(target_words)
    for word in target_words:
        expanded_target_words |= FIELD_CONCEPT_SYNONYMS.get(word, set())
    for line in lines:
        m = re.match(r"^([A-Za-z0-9 _/-]+):\s*(.+)$", line)
        if not m:
            continue
        label_words = _normalised_field_words(m.group(1))
        if expanded_target_words & label_words:
            value = m.group(2).strip()
            if _needs_generated_email(value):
                if not run_id:
                    return None, "generated email requires a run id"
                value = _generated_email(run_id)
            return value, f"matched Test Data label '{m.group(1).strip()}'"

    # No label shares a word with the target -- but some suites label Test
    # Data by scenario rather than by field name (e.g. "Valid: x@y.com" /
    # "Invalid: abc" for the same email field, used across two separate
    # steps). When the target itself names a generic content type ("email"),
    # matching by that type's shape (e.g. "contains @") rather than by label
    # text is still a deterministic, content-based match -- not a guess at
    # an arbitrary value -- and works for any app's Test Data authored this
    # way, not just one specific wording convention.
    if "email" in target_words:
        for line in lines:
            m = re.match(r"^([A-Za-z0-9 _/-]+):\s*(.+)$", line)
            value = (m.group(2).strip() if m else line)
            if "@" in value:
                label = m.group(1).strip() if m else "sole line"
                return value, f"matched Test Data line '{label}' by email shape (no label matched by name)"
    return None, "no matching Test Data label found"


def _normalised_field_words(value: str) -> set[str]:
    words = {word.lower() for word in re.findall(r"[A-Za-z]+", value)}
    aliases = {
        "zipcode": "zip",
        "postal": "zip",
        "mobile_number": "mobile",
        "number": "number",
    }
    return {aliases.get(word, word) for word in words}


def _field_action(target: str) -> str:
    """Choose a generic control action from a workbook field description."""
    normalized = target.lower()
    if normalized in {"title", "gender"}:
        return "check"
    if normalized in {"dob", "day", "month", "year", "country"} or normalized.startswith("dob "):
        return "select"
    return "type"


def _expand_field_target(target: str, test_data: str) -> list[tuple[str, str]]:
    """Expand composite form fields into independently resolvable controls."""
    normalized = target.strip().lower()
    if normalized == "dob":
        value, detail = _extract_typed_value("dob", test_data)
        if value is None:
            return [(target, detail)]
        match = re.fullmatch(r"(\d{1,2})[-/ ]([A-Za-z]+|\d{1,2})[-/ ](\d{4})", value.strip())
        if not match:
            return [(target, f"unsupported DOB format {value!r}")]
        day, month, year = match.groups()
        month_value = month
        try:
            month_value = str(datetime.strptime(month.title(), "%B").month)
        except ValueError:
            pass
        return [("DOB day", day), ("DOB month", month_value), ("DOB year", year)]
    if normalized == "first/last name":
        return [("First name", ""), ("Last name", "")]
    if normalized in _CARD_DETAILS_NAMES:
        # "Enter card details" is a generic, collective instruction common to
        # any checkout form, not naming each field individually the way
        # "Fill name, email" does -- each of these is independently matched
        # against the row's own Test Data labels by _extract_typed_value
        # (via its existing word/synonym matching), so a suite using
        # different label wording ("Card Number" vs "Card No") still works.
        fields = [("Name on Card", ""), ("Card Number", ""), ("CVC", "")]
        expiry_value, _ = _extract_typed_value("expiry", test_data)
        # A combined "MM/YYYY" Test Data value must become two separate
        # values -- the real form (like DOB above) splits expiry into its own
        # month and year inputs, not one combined field. If there's no usable
        # value, or it's not in that shape, fall back to a plain single
        # "Expiry" field so the normal (unexpanded) Test Data/Blocked path
        # still runs rather than typing a parse-error message as if it were
        # the field's value.
        expiry_match = re.fullmatch(r"(\d{1,2})[-/](\d{4})", expiry_value.strip()) if expiry_value else None
        if expiry_match:
            month, year = expiry_match.groups()
            fields += [("Expiry Month", month), ("Expiry Year", year)]
        else:
            fields.append(("Expiry", ""))
        return fields
    return [(target, "")]


# Recognized as a stand-in for a checkout form's whole card-details group --
# generic phrasing any e-commerce test suite might use, not one app's wording.
_CARD_DETAILS_NAMES = {"card details", "card information", "payment details", "payment information"}


def _needs_generated_email(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {
        "${generated_email}",
        "{{generated_email}}",
        "<generated_email>",
        "<registered email>",
        "<previously registered email>",
    } or (
        "<timestamp>" in normalized
        and "@" in normalized
        and "email" not in normalized
    )


def _generated_email(run_id: str) -> str:
    """Return one deterministic synthetic address shared by all rows in a run."""
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, run_id).hex[:12]
    return f"qa.tester+{suffix}@test.com"


_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}

_EMAIL_LINE_RE = re.compile(r"(?im)^\s*email\s*:\s*(.+)$")
_PASSWORD_LINE_RE = re.compile(r"(?im)^\s*password\s*:\s*(.+)$")
_NAME_LINE_RE = re.compile(r"(?im)^\s*name\s*:\s*(.+)$")


def _maybe_cache_account(
    row: TestCaseRow, page: Page, run_id: str, application: str, environment: str, account_cache_dir
) -> None:
    """After a row's sub-steps all ran cleanly (no Blocked/Fail), remember its
    Email/Password as the working test account for this app+environment, so a
    later row's bare "Login with valid credentials" can actually log in
    instead of being reported Blocked as an unresolvable precondition.

    Text in Test Data alone can't tell a real account from a deliberately
    wrong one -- "Login with invalid password" has a real-looking email and a
    password with no "<...>" placeholder either, same shape as a genuine
    account. The reliable, app-agnostic signal is page state: a standard
    HTML5 input[type="password"] still visible means we're still looking at
    a login/signup form, which only happens when the credentials were
    rejected (or never submitted at all, e.g. a deliberately blank-fields
    test). Only cache once that form is actually gone.
    """
    email_match = _EMAIL_LINE_RE.search(row.test_data)
    password_match = _PASSWORD_LINE_RE.search(row.test_data)
    if not email_match or not password_match:
        print("DEBUGXYZ no email/password match in test_data")
        return
    email = email_match.group(1).strip()
    if _needs_generated_email(email):
        email = _generated_email(run_id)
    password = password_match.group(1).strip()
    if "<" in email or ">" in email or "@" not in email or len(password) < 1 or "<" in password or ">" in password:
        print("DEBUGXYZ email/password looks invalid:", repr(email), repr(password))
        return
    try:
        pw_visible = page.locator("input[type='password']").visible_count()
        print("DEBUGXYZ url=", page.url, "password_visible_count=", pw_visible)
        if pw_visible > 0:
            return
    except Exception as exc:
        print("DEBUGXYZ exception checking password visibility:", exc)
        return
    name_match = _NAME_LINE_RE.search(row.test_data)
    name = name_match.group(1).strip() if name_match else None
    print("DEBUGXYZ saving account:", email, account_cache_dir)
    save_account(account_cache_dir, application, environment, email=email, password=password, name=name)


def execute_test_case(
    row: TestCaseRow,
    *,
    page: Page,
    store: ElementStore,
    review_queue: ReviewQueue,
    llm_client: LlmClient | None,
    allow_guard: AllowListGuard,
    application: str,
    environment: str,
    page_name: str,
    base_url: str,
    run_id: str,
    user_id: str,
    min_similarity: float,
    min_confidence: float,
    healing_auto_approve: bool,
    action_timeout_ms: int,
    navigation_timeout_ms: int,
    console: ConsoleNarrator,
    usage_logger: UsageLogger | None = None,
    llm_trace: bool = False,
    login_steps: list | None = None,
    registration_steps: list | None = None,
    login_success_text: str | None = None,
    test_account_env_vars: dict | None = None,
    account_cache_dir: str | Path | None = None,
) -> RowExecutionResult:
    started = time.monotonic()
    sub_steps = parse_test_steps(row.test_step)
    console.step_start(row.test_case_id, 0, "test case", row.title)

    details: list[str] = []
    worst_status = "Pass"
    element_sources: set[str] = set()
    tokens_total = 0
    llm_model: str | None = None

    # Parsed once, up front, so probing after each sub-step and the final
    # evaluation both index into the exact same check list.
    expected_checks = parse_expected_result(
        _substitute_expected_result_placeholders(row.expected_result, row.test_data, run_id)
    )
    checks_satisfied = [False] * len(expected_checks)

    def run_step(target_sub: SubStep) -> tuple[str, str, str | None, int, str | None]:
        return _run_sub_step(
            row, target_sub, page=page, store=store, review_queue=review_queue, llm_client=llm_client,
            allow_guard=allow_guard, application=application, environment=environment,
            page_name=page_name, base_url=base_url, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
            min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
            action_timeout_ms=action_timeout_ms, navigation_timeout_ms=navigation_timeout_ms,
            console=console, usage_logger=usage_logger, llm_trace=llm_trace,
            login_steps=login_steps, registration_steps=registration_steps,
            login_success_text=login_success_text,
            test_account_env_vars=test_account_env_vars,
            account_cache_dir=account_cache_dir,
        )

    last_type_sub: SubStep | None = None
    last_click_sub: SubStep | None = None

    for sub in sub_steps:
        if sub.action == "repeat_with_value":
            if last_type_sub is None:
                outcome_status, outcome_detail, source, tokens, model = (
                    "Blocked", "no earlier 'type' step in this row to repeat with a new value", None, 0, None,
                )
            else:
                synthetic_type = replace(last_type_sub, literal_value=sub.literal_value)
                outcome_status, outcome_detail, source, tokens, model = run_step(synthetic_type)
                outcome_detail = f"repeated type with {sub.literal_value!r}: {outcome_detail}"
                if outcome_status == "Pass" and last_click_sub is not None:
                    c_status, c_detail, c_source, c_tokens, c_model = run_step(last_click_sub)
                    tokens += c_tokens
                    model = c_model or model
                    source = c_source or source
                    outcome_detail = f"{outcome_detail} | repeated click: {c_detail}"
                    if _STATUS_RANK[c_status] > _STATUS_RANK[outcome_status]:
                        outcome_status = c_status
        else:
            outcome_status, outcome_detail, source, tokens, model = run_step(sub)
            if sub.action == "type" and outcome_status == "Pass":
                last_type_sub = sub
            if sub.action == "click" and outcome_status == "Pass":
                last_click_sub = sub
        # Check for popups/overlays after every step too, not just before the
        # next one's element interaction -- the step just taken (a click, a
        # navigation) is exactly what commonly triggers one (an ad, a modal,
        # a cookie banner) to appear in the first place. Wait for the page to
        # finish settling first: a click that submits a form triggers a full
        # navigation that is often still in flight immediately after the
        # click call returns, and running overlay-clearing JS (which can
        # click any on-screen element matching a generic close-button
        # pattern) against a mid-navigation page risks clicking something
        # that belongs to the page being left, interfering with the
        # navigation itself rather than cleaning up after it.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=action_timeout_ms)
        except Exception:
            pass
        try:
            clear_overlays_before_interaction(page, action_timeout_ms)
        except Exception:
            pass
        if outcome_status == "Pass" and expected_checks:
            try:
                _probe_expected_checks_progress(expected_checks, checks_satisfied, page)
            except Exception:
                pass
        details.append(f"step {sub.step_no} ({sub.raw_line!r}): {outcome_status} - {outcome_detail}")
        tokens_total += tokens
        if model:
            llm_model = model
        if source:
            element_sources.add(source)
        if _STATUS_RANK[outcome_status] > _STATUS_RANK[worst_status]:
            worst_status = outcome_status
        console.step_result(row.test_case_id, sub.step_no, outcome_status)
        if outcome_status in ("Blocked", "Fail"):
            details.append(f"remaining sub-steps skipped after step {sub.step_no}")
            break
    else:
        # All sub-steps ran without Blocked/Fail -> evaluate Expected Result.
        exp_status, exp_detail = _evaluate_expected_result(
            row, page, action_timeout_ms, run_id, pre_satisfied=checks_satisfied
        )
        details.append(exp_detail)
        if _STATUS_RANK.get(exp_status, 0) > _STATUS_RANK[worst_status]:
            worst_status = exp_status

    # Deliberately OUTSIDE the for/else above, so it still runs even when an
    # unrelated LATER step blocks/fails: confirmed live (automationexercise.com)
    # that a registration row can genuinely complete signup (account created,
    # usable right now on the live site) and then block on a later, unrelated
    # step (e.g. a confirmation page's "Continue" button) -- gating this
    # behind "the whole row passed" threw away a real, now-orphaned account
    # that nothing else could ever reference again, leaving every later row
    # that needed "a previously registered account" with nothing to use.
    # _maybe_cache_account's own internal check (no password field still
    # visible) is already the real signal for "did this actually create/log
    # into an account", independent of what happens afterward.
    if account_cache_dir is not None:
        _maybe_cache_account(row, page, run_id, application, environment, account_cache_dir)

    execution_time_ms = int((time.monotonic() - started) * 1000)
    executed_at = datetime.now(timezone.utc).isoformat()

    return RowExecutionResult(
        test_case_id=row.test_case_id,
        status=worst_status,
        actual_result=" | ".join(details),
        element_source=",".join(sorted(element_sources)) or "None",
        tokens_used=tokens_total,
        llm_model=llm_model,
        execution_time_ms=execution_time_ms,
        executed_at=executed_at,
    )


def _run_multi_click(
    row: TestCaseRow, sub: SubStep, click_targets: list[str], *, page, store, review_queue,
    llm_client, application, environment, page_name, run_id, user_id, min_similarity,
    min_confidence, healing_auto_approve, action_timeout_ms, console, usage_logger, llm_trace=False,
) -> tuple[str, str, str | None, int, str | None]:
    """Resolve and click each target in a 'Click A > B' breadcrumb-style
    sequence in order -- A reveals or leads to B, so they're two real
    actions, not one element matching the whole phrase."""
    details: list[str] = []
    tokens_used = 0
    llm_model: str | None = None
    element_sources: set[str] = set()

    for click_target in click_targets:
        outcome = resolve_element(
            store=store, review_queue=review_queue, llm_client=llm_client, page=page,
            application=application, environment=environment, target_page=_page_key(page, page_name),
            description=click_target, action="click", run_id=run_id,
            test_case_id=row.test_case_id, step_no=sub.step_no, user_id=user_id,
            min_similarity=min_similarity, min_confidence=min_confidence,
            healing_auto_approve=healing_auto_approve, console=console, usage_logger=usage_logger,
            llm_trace=llm_trace,
        )
        tokens_used += outcome.tokens_used
        if outcome.llm_model:
            llm_model = outcome.llm_model
        if outcome.status != "Resolved":
            details.append(f"{click_target!r}: Blocked - {outcome.reason}")
            return "Blocked", " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model
        if outcome.element_source:
            element_sources.add(outcome.element_source)

        console.action(row.test_case_id, sub.step_no, "click", outcome.selector)
        try:
            result = verbs.click(page, outcome.selector, action_timeout_ms)
        except Exception as exc:
            details.append(f"{click_target!r}: Fail - action raised: {exc}")
            return "Fail", " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model
        if not result.ok:
            details.append(f"{click_target!r}: Fail - {result.detail}")
            return "Fail", " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model
        details.append(f"{click_target!r}: Pass - {result.detail}")

        # The first click in the sequence commonly navigates or reveals new
        # content (e.g. "Products" -> listing page) that the next click's
        # target depends on; give it a moment to settle before resolving
        # the next one, same reasoning as the post-step overlay check.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=action_timeout_ms)
        except Exception:
            pass
        try:
            clear_overlays_before_interaction(page, action_timeout_ms)
        except Exception:
            pass

    if sub.dialog_after:
        dialog_result = verbs.accept_dialog(page, action_timeout_ms)
        details.append(dialog_result.detail)
        if not dialog_result.ok:
            return "Fail", " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model

    return "Pass", " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model


def _run_multi_field_type(
    row: TestCaseRow, sub: SubStep, field_targets: list[str], *, page, store, review_queue,
    llm_client, application, environment, page_name, run_id, user_id, min_similarity,
    min_confidence, healing_auto_approve, action_timeout_ms, console, usage_logger, llm_trace=False,
) -> tuple[str, str, str | None, int, str | None]:
    """Resolve and type into each field of a 'Fill A, B, C' step separately.
    Status is the worst outcome across fields (Fail > Blocked > Pass) -- a
    field that could not be resolved or typed into is never silently reported
    as an overall Pass."""
    details: list[str] = []
    tokens_used = 0
    element_sources: set[str] = set()
    llm_model: str | None = None
    worst_status = "Pass"

    expanded_targets: list[tuple[str, str | None]] = []
    for field_target in field_targets:
        expanded_targets.extend(
            (expanded_target, expanded_value or None)
            for expanded_target, expanded_value in _expand_field_target(field_target, row.test_data)
        )

    for field_target, expanded_value in expanded_targets:
        value, value_detail = (
            (expanded_value, "expanded composite field")
            if expanded_value is not None
            else _extract_typed_value(field_target, row.test_data, run_id=run_id)
        )
        if value is None:
            details.append(f"{field_target}: Blocked - {value_detail}")
            worst_status = _worse(worst_status, "Blocked")
            continue

        resolution_description = _normalise_type_target(field_target)
        if sub.context:
            resolution_description = f"{field_target} in {sub.context}"
        control_action = _field_action(field_target)
        if control_action == "check" and value:
            # Radio/checkbox groups (e.g. "Title": Mr vs Mrs) have more than one
            # option element sharing the same bare field name; unlike `select`
            # (where the resolver locates one <select> and select_option() then
            # picks the value inside it), `check` only clicks whatever single
            # element it's given -- so the desired option must be in the
            # description itself, or the AI has no way to tell which one is wanted.
            resolution_description = f"{resolution_description}: {value}"
        field_outcome = resolve_element(
            store=store, review_queue=review_queue, llm_client=llm_client, page=page,
            application=application, environment=environment, target_page=_page_key(page, page_name),
            description=resolution_description, action=control_action, run_id=run_id, test_case_id=row.test_case_id,
            step_no=sub.step_no, user_id=user_id, min_similarity=min_similarity,
            min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
            console=console, usage_logger=usage_logger, llm_trace=llm_trace,
        )
        tokens_used += field_outcome.tokens_used
        if field_outcome.llm_model:
            llm_model = field_outcome.llm_model
        if field_outcome.status != "Resolved":
            details.append(f"{field_target}: Blocked - {field_outcome.reason}")
            worst_status = _worse(worst_status, "Blocked")
            continue
        if field_outcome.element_source:
            element_sources.add(field_outcome.element_source)

        console.action(row.test_case_id, sub.step_no, control_action, field_outcome.selector)
        try:
            if control_action == "select":
                type_result = verbs.select_option(page, field_outcome.selector, value, action_timeout_ms)
            elif control_action == "check":
                type_result = verbs.check(page, field_outcome.selector, action_timeout_ms)
            else:
                type_result = verbs.type_text(page, field_outcome.selector, value, action_timeout_ms)
        except Exception as exc:
            details.append(f"{field_target}: Fail - action raised: {exc}")
            worst_status = _worse(worst_status, "Fail")
            continue

        if type_result.ok:
            details.append(f"{field_target}: Pass - {type_result.detail}")
        else:
            details.append(f"{field_target}: Fail - {type_result.detail}")
            worst_status = _worse(worst_status, "Fail")

    return worst_status, " | ".join(details), ",".join(sorted(element_sources)) or None, tokens_used, llm_model


def _worse(a: str, b: str) -> str:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


def _collapse_target_already_expanded(page: Page, selector: str) -> bool:
    """True if `selector` is a Bootstrap data-toggle="collapse" element whose
    target panel is already expanded OR actively mid-expansion. False for
    anything else (not a collapse toggle, target missing, or genuinely still
    closed) -- callers only need to know "is a retry-click redundant right
    now", not distinguish why.

    'collapsing' counts as already-triggered, not just 'in'/'show':
    confirmed live that right after the click this check runs for, the
    panel can still be mid-CSS-transition (class="panel-collapse
    collapsing") rather than fully 'in' yet -- treating only 'in'/'show' as
    "already expanded" would miss that window and let a redundant retry
    click interrupt/reverse a transition that was already under way.
    """
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el || el.getAttribute('data-toggle') !== 'collapse') return false;
                const target = el.getAttribute('data-target') || el.getAttribute('href');
                if (!target || !target.startsWith('#')) return false;
                const t = document.querySelector(target);
                if (!t) return false;
                return t.classList.contains('in') || t.classList.contains('show')
                    || t.classList.contains('collapsing');
            }""",
            selector,
        )
    except Exception:
        return False
    return bool(result)


def _page_key(page: Page, fallback: str) -> str:
    """Derive the element store's partition key from the live page's URL path
    rather than trusting a single CLI-wide --page-name for the whole suite.

    The store key is (application, environment, page, description); a suite
    visits many real pages (login, account info, products, cart, ...), and
    partitioning them all under one static label means a learned element on
    one page is invisible to a later run that happened to pass a different
    --page-name, forcing needless re-learning (or worse, collisions between
    two different elements that share a description across pages). The first
    URL path segment is normally the site's own name for that page/section.
    """
    try:
        path = urlparse(page.url).path.strip("/")
    except Exception:
        return fallback
    if not path:
        return fallback
    return path.split("/")[0] or fallback


@dataclass
class _DeclarativeStepsOutcome:
    status: str
    detail: str
    element_sources: set[str]
    tokens_used: int
    llm_model: str | None
    password_field_selector: str | None


def _run_declarative_steps(
    steps: list, email: str, password: str, label: str, *, row, sub, page, store, review_queue,
    llm_client, allow_guard, application, environment, page_name, base_url, run_id, user_id,
    min_similarity, min_confidence, healing_auto_approve, action_timeout_ms, navigation_timeout_ms,
    console, usage_logger, llm_trace,
) -> _DeclarativeStepsOutcome:
    """Mechanically run one declarative step list (an app's login_steps or
    registration_steps -- same shape, same engine) with the given
    credentials substituted in. Shared by _run_auto_login for both its login
    attempt and its registration fallback, so the two never drift apart.
    """
    element_sources: set[str] = set()
    tokens_used = 0
    llm_model: str | None = None
    password_field_selector: str | None = None

    for i, step in enumerate(steps, start=1):
        data_value = step.data
        is_password_step = bool(data_value and "${TEST_ACCOUNT_PASSWORD}" in data_value)
        if data_value:
            data_value = data_value.replace("${TEST_ACCOUNT_EMAIL}", email).replace(
                "${TEST_ACCOUNT_PASSWORD}", password
            )

        if step.action == "navigate":
            target_url = resolve_navigate_target(step.target, base_url)
            if target_url is not None:
                try:
                    result = verbs.navigate(page, target_url, allow_guard, navigation_timeout_ms)
                except Exception as exc:
                    return _DeclarativeStepsOutcome("Fail", f"{label} navigate raised: {exc}", element_sources, tokens_used, llm_model, None)
                if not result.ok:
                    return _DeclarativeStepsOutcome("Blocked", f"{label} navigate failed: {result.detail}", element_sources, tokens_used, llm_model, None)
                clear_overlays_before_interaction(page, action_timeout_ms)
                continue
            # target_url is None: the target names a specific destination that
            # isn't a literal URL and isn't home (e.g. "Signup / Login page")
            # -- fall through to the generic resolver and click whatever link
            # leads there, same as a plain "click" step would.

        clear_overlays_before_interaction(page, action_timeout_ms)
        resolver_action = "click" if step.action == "navigate" else {"click": "click", "type": "type", "select": "select", "check": "check"}.get(step.action)
        outcome = resolve_element(
            store=store, review_queue=review_queue, llm_client=llm_client, page=page,
            application=application, environment=environment, target_page=_page_key(page, page_name),
            description=step.target, action=resolver_action, run_id=run_id,
            test_case_id=row.test_case_id, step_no=sub.step_no, user_id=user_id,
            min_similarity=min_similarity, min_confidence=min_confidence,
            healing_auto_approve=healing_auto_approve, console=console, usage_logger=usage_logger,
            llm_trace=llm_trace,
        )
        tokens_used += outcome.tokens_used
        if outcome.llm_model:
            llm_model = outcome.llm_model
        if outcome.status != "Resolved":
            return _DeclarativeStepsOutcome(
                "Blocked", f"{label} step {i} ({step.action} '{step.target}'): {outcome.reason}",
                element_sources, tokens_used, llm_model, None,
            )
        if outcome.element_source:
            element_sources.add(outcome.element_source)
        if is_password_step:
            password_field_selector = outcome.selector

        try:
            if step.action == "type":
                console.action(row.test_case_id, sub.step_no, "type", outcome.selector)
                result = verbs.type_text(page, outcome.selector, data_value or "", action_timeout_ms)
            elif step.action in ("click", "navigate"):
                console.action(row.test_case_id, sub.step_no, "click", outcome.selector)
                result = verbs.click(page, outcome.selector, action_timeout_ms)
            elif step.action == "select":
                console.action(row.test_case_id, sub.step_no, "select", outcome.selector)
                result = verbs.select_option(page, outcome.selector, data_value or "", action_timeout_ms)
            elif step.action == "check":
                console.action(row.test_case_id, sub.step_no, "check", outcome.selector)
                result = verbs.check(page, outcome.selector, action_timeout_ms)
            else:
                return _DeclarativeStepsOutcome(
                    "Blocked", f"unsupported {label} action '{step.action}'", element_sources, tokens_used, llm_model, None,
                )
        except Exception as exc:
            return _DeclarativeStepsOutcome("Fail", f"{label} step {i} raised: {exc}", element_sources, tokens_used, llm_model, None)
        if not result.ok:
            return _DeclarativeStepsOutcome("Fail", f"{label} step {i} failed: {result.detail}", element_sources, tokens_used, llm_model, None)

    return _DeclarativeStepsOutcome("Pass", f"{label} steps completed", element_sources, tokens_used, llm_model, password_field_selector)


def _still_showing_password_field(page: Page, password_field_selector: str | None) -> bool:
    """A generic, app-agnostic way to tell whether a login attempt was
    accepted: if the exact password field just typed into is still visible
    afterward, the same login form is still being shown, which only happens
    when the site rejected the credentials.

    Checked with a brief, bounded settle window rather than the instant the
    login click returns: on a client-side-routed SPA (confirmed live on
    saucedemo.com -- mechanically identical login steps, immediately
    verified to have genuinely succeeded via a separate check elsewhere in
    the same run, were intermittently reported rejected here), the old
    page's DOM can still be present for a brief moment after a successful
    submit, before the route transition actually swaps it out. A real
    rejection leaves the field visible well past this window; this only
    changes how long "still showing" is given to resolve itself, never
    whether a genuine rejection is still correctly detected.
    """
    if not password_field_selector:
        return False
    try:
        locator = page.locator(password_field_selector)
        locator.wait_for(state="hidden", timeout=2000)
        return False
    except Exception:
        try:
            return page.locator(password_field_selector).is_visible()
        except Exception:
            return False


def _run_auto_login(
    row: TestCaseRow, sub: SubStep, *, page, store, review_queue, llm_client, allow_guard,
    application, environment, page_name, base_url, run_id, user_id, min_similarity, min_confidence,
    healing_auto_approve, action_timeout_ms, navigation_timeout_ms, console, usage_logger,
    llm_trace, login_steps, test_account_env_vars, account_cache_dir, registration_steps=None,
    login_success_text=None, verify_success=True,
) -> tuple[str, str, str | None, int, str | None]:
    """Perform a real login using the app's declarative login_steps (see
    apps/<name>/config.yaml) and whichever credentials are available -- a
    cached account from an earlier successful registration this run (or a
    prior run), or the app's configured test-account env vars. Never invents
    credentials: if neither source has any, this is Blocked, not guessed.

    verify_success=False (used by the "attempt_login" action, for steps like
    "Try logging in again with the same credentials" on a deleted account)
    skips the final acceptance check -- and, deliberately, the registration
    fallback below: the step's whole point there is to confirm the rejection
    itself, so silently creating a fresh account would defeat the test.
    """
    if not login_steps:
        return "Blocked", "no declarative login_steps configured for this application", None, 0, None

    email = password = None
    account = load_account(account_cache_dir, application, environment) if account_cache_dir is not None else None
    if account:
        email, password = account.get("email"), account.get("password")
    elif test_account_env_vars:
        email = os.environ.get(test_account_env_vars.get("email", ""))
        password = os.environ.get(test_account_env_vars.get("password", ""))

    if not email or not password:
        if verify_success and registration_steps:
            email, password = _generated_email(run_id), "Test@1234"
        else:
            return (
                "Blocked",
                "no cached test account and no usable test account env vars configured; "
                "a row that registers an account must run earlier in this suite, or "
                "TEST_ACCOUNT_EMAIL/TEST_ACCOUNT_PASSWORD must be set",
                None, 0, None,
            )

    kwargs = dict(
        row=row, sub=sub, page=page, store=store, review_queue=review_queue, llm_client=llm_client,
        allow_guard=allow_guard, application=application, environment=environment, page_name=page_name,
        base_url=base_url, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
        min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
        action_timeout_ms=action_timeout_ms, navigation_timeout_ms=navigation_timeout_ms,
        console=console, usage_logger=usage_logger, llm_trace=llm_trace,
    )
    outcome = _run_declarative_steps(login_steps, email, password, "auto-login", **kwargs)
    rejected = verify_success and outcome.status == "Pass" and _still_showing_password_field(page, outcome.password_field_selector)

    if rejected and registration_steps:
        # The account this row would have used doesn't work (most commonly:
        # an earlier row deleted it) -- rather than report that honestly but
        # unhelpfully as Blocked every time, establish a fresh one the same
        # way a human re-running this suite would, then retry the login once.
        console.line(f"[{row.test_case_id}] login rejected for {email}; registering a fresh account before retrying")
        reg_outcome = _run_declarative_steps(registration_steps, email, password, "auto-registration", **kwargs)
        total_tokens = outcome.tokens_used + reg_outcome.tokens_used
        combined_sources = outcome.element_sources | reg_outcome.element_sources
        combined_model = reg_outcome.llm_model or outcome.llm_model
        if reg_outcome.status != "Pass":
            return (
                "Blocked", f"login was rejected and registering a replacement account also failed: {reg_outcome.detail}",
                ",".join(sorted(combined_sources)) or None, total_tokens, combined_model,
            )
        # Some sites (this one included) leave a freshly registered account
        # already signed in -- re-running login_steps there is not just
        # redundant but actively wrong, since the "Signup / Login" link it
        # would click no longer exists once authenticated. Where the app
        # config gives a positive signal for that state, trust it and skip
        # the retry; apps with no such signal fall through to the retry
        # below, which remains the correct default for sites that do require
        # a separate login after registering.
        if login_success_text and page.get_by_text(login_success_text).count() > 0:
            if account_cache_dir is not None:
                save_account(account_cache_dir, application, environment, email=email, password=password)
            return (
                "Pass", f"registered a fresh account for {email}, already signed in afterward",
                ",".join(sorted(combined_sources)) or None, total_tokens, combined_model,
            )
        retry_outcome = _run_declarative_steps(login_steps, email, password, "auto-login (retry after registration)", **kwargs)
        total_tokens += retry_outcome.tokens_used
        combined_sources |= retry_outcome.element_sources
        combined_model = retry_outcome.llm_model or combined_model
        if retry_outcome.status != "Pass" or _still_showing_password_field(page, retry_outcome.password_field_selector):
            return (
                "Fail", f"registered a fresh account for {email} but the retried login was still rejected",
                ",".join(sorted(combined_sources)) or None, total_tokens, combined_model,
            )
        if account_cache_dir is not None:
            save_account(account_cache_dir, application, environment, email=email, password=password)
        return (
            "Pass", f"registered a fresh account and logged in as {email}",
            ",".join(sorted(combined_sources)) or None, total_tokens, combined_model,
        )

    if rejected:
        return (
            "Fail",
            f"auto-login steps completed but the login form is still showing afterward "
            f"-- credentials for {email} were not accepted",
            ",".join(sorted(outcome.element_sources)) or None, outcome.tokens_used, outcome.llm_model,
        )
    if outcome.status != "Pass":
        return outcome.status, outcome.detail, ",".join(sorted(outcome.element_sources)) or None, outcome.tokens_used, outcome.llm_model

    login_detail = f"logged in as {email}" if verify_success else f"attempted login as {email} (outcome judged by Expected Result)"
    return "Pass", login_detail, ",".join(sorted(outcome.element_sources)) or None, outcome.tokens_used, outcome.llm_model


def _run_sub_step(
    row: TestCaseRow, sub: SubStep, *, page, store, review_queue, llm_client, allow_guard,
    application, environment, page_name, base_url, run_id, user_id, min_similarity, min_confidence,
    healing_auto_approve, action_timeout_ms, navigation_timeout_ms, console, usage_logger=None,
    llm_trace=False, login_steps=None, registration_steps=None, login_success_text=None,
    test_account_env_vars=None, account_cache_dir=None,
) -> tuple[str, str, str | None, int, str | None]:
    if sub.action == "noop":
        return "Pass", "no-op step, nothing to do", None, 0, None

    if sub.action in ("auto_login", "attempt_login"):
        return _run_auto_login(
            row, sub, page=page, store=store, review_queue=review_queue, llm_client=llm_client,
            allow_guard=allow_guard, application=application, environment=environment,
            page_name=page_name, base_url=base_url, run_id=run_id, user_id=user_id,
            min_similarity=min_similarity, min_confidence=min_confidence,
            healing_auto_approve=healing_auto_approve, action_timeout_ms=action_timeout_ms,
            navigation_timeout_ms=navigation_timeout_ms, console=console, usage_logger=usage_logger,
            llm_trace=llm_trace, login_steps=login_steps, test_account_env_vars=test_account_env_vars,
            account_cache_dir=account_cache_dir, registration_steps=registration_steps,
            login_success_text=login_success_text, verify_success=(sub.action == "auto_login"),
        )

    if sub.action == "unparseable":
        # step_parser sets `target` to an explanatory message for recognized-but-
        # unsupported shapes (e.g. cross-test-case preconditions); fall back to
        # the generic grammar-failure message otherwise.
        detail = (
            f"{sub.target} (step: {sub.raw_line!r})" if sub.target != sub.raw_line
            else f"could not parse step grammar: {sub.raw_line!r}"
        )
        return "Blocked", detail, None, 0, None

    if sub.action == "navigate":
        target_url = resolve_navigate_target(sub.target, base_url)
        if target_url is not None:
            try:
                result = verbs.navigate(page, target_url, allow_guard, navigation_timeout_ms)
            except Exception as exc:
                # A transient browser/network error here (e.g. a renderer timeout
                # talking to a real, external site) must not crash the whole run
                # and lose every row's results -- same reasoning as the
                # try/except already around the other action kinds below.
                return "Fail", f"navigate raised: {exc}", None, 0, None
            if result.ok:
                # Dismiss any ad overlays that may have appeared on the new page
                clear_overlays_before_interaction(page, action_timeout_ms)
            return ("Pass" if result.ok else "Blocked"), result.detail, None, 0, None
        # target_url is None: the target names a specific destination that
        # isn't a literal URL and isn't home -- fall through to the generic
        # resolver below and click whatever link leads there.

    # Before attempting any element interaction, dismiss any ad overlays
    clear_overlays_before_interaction(page, action_timeout_ms)

    resolver_action_map = {
        "click": "click", "hover": "hover", "type": "type", "select": "select", "check": "check",
        "upload": "click", "accept_dialog": "click", "assert_visible": "assert_visible",
        "navigate": "click",
    }
    resolver_action = resolver_action_map.get(sub.action)
    if resolver_action is None:
        return "Blocked", f"unsupported action '{sub.action}'", None, 0, None

    if sub.action == "assert_visible":
        # Checkpoints inside the step list ("Observe/Verify ...") are informational
        # only; the authoritative check is Expected Result, evaluated once at the
        # end of the row -- never guessed from a free-text mid-step line.
        return "Pass", f"checkpoint noted: {sub.target}", None, 0, None

    # Multi-field "Fill A, B, C" steps resolve and type into each field
    # separately -- a single resolve_element call against the whole
    # comma-joined description would never match one element, so this must be
    # checked before the single-target resolve below, not after it.
    if sub.action == "type":
        field_targets = split_multi_field_targets(sub.target)
        is_composite_single = (
            len(field_targets) == 1 and field_targets[0].strip().lower() in _CARD_DETAILS_NAMES
        )
        if len(field_targets) > 1 or is_composite_single:
            return _run_multi_field_type(
                row, sub, field_targets, page=page, store=store, review_queue=review_queue,
                llm_client=llm_client, application=application, environment=environment,
                page_name=page_name, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
                min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
                action_timeout_ms=action_timeout_ms, console=console, usage_logger=usage_logger,
                llm_trace=llm_trace,
            )

    # "Click 'A' > 'B'" breadcrumb-style steps resolve and click each target
    # in sequence -- same reasoning as the multi-field case above, for clicks.
    if sub.action == "click" and " > " in sub.target:
        click_targets = [t.strip() for t in sub.target.split(" > ") if t.strip()]
        if len(click_targets) > 1:
            return _run_multi_click(
                row, sub, click_targets, page=page, store=store, review_queue=review_queue,
                llm_client=llm_client, application=application, environment=environment,
                page_name=page_name, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
                min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
                action_timeout_ms=action_timeout_ms, console=console, usage_logger=usage_logger,
                llm_trace=llm_trace,
            )

    resolution_description = _normalise_type_target(sub.target)
    if sub.context:
        resolution_description = f"{sub.target} in {sub.context}"
    # Checked regardless of whether sub.context just set resolution_description
    # above: "Click 'Add to cart' for the first product" now carries "the
    # first product" as context (step_parser's "for" connector), and the
    # ordinal/Test-Data enrichment below has to see "product" wherever it
    # ended up -- target or context -- not just when there's no context at
    # all, or a step whose "which product" qualifier came in via context
    # would be left just as ambiguous as before that connector existed.
    if re.search(r"(?i)\bproduct\b", resolution_description):
        # Word-boundary, not substring: a bare nav link target like
        # "Products" (plural) must NOT trigger this -- it has nothing to do
        # with "which specific product", and wrongly enriching it broke the
        # plain "Click 'Products'" step.
        # A step naming "product" generically (e.g. "product detail page",
        # "second product") relies on Test Data to say *which* product --
        # without this, the target is ambiguous among every product on a
        # listing page the same way "Title" needed its Test Data value
        # folded in for a radio group.
        ordinal_match = re.search(r"(?i)\b(first|second|third|fourth|fifth)\b", resolution_description)
        product_name = None
        if ordinal_match:
            # "second product" -> Test Data's "Product 2: <name>" line. Ordinal
            # words are generic English, not tied to any one app's wording.
            number = _ORDINAL_WORDS.get(ordinal_match.group(1).lower())
            numbered_match = re.search(rf"(?im)^\s*product\s*{number}\s*:\s*(.+)$", row.test_data)
            if numbered_match:
                product_name = numbered_match.group(1).strip()
        if product_name is None:
            product_match = re.search(r"(?im)^\s*product\s*:\s*(.+)$", row.test_data)
            if product_match:
                product_name = product_match.group(1).strip()
        if product_name:
            resolution_description = f"{resolution_description}: {product_name}"
    outcome = resolve_element(
        store=store, review_queue=review_queue, llm_client=llm_client, page=page,
        application=application, environment=environment, target_page=_page_key(page, page_name),
        description=resolution_description, action=resolver_action, run_id=run_id,
        test_case_id=row.test_case_id, step_no=sub.step_no, user_id=user_id,
        min_similarity=min_similarity, min_confidence=min_confidence,
        healing_auto_approve=healing_auto_approve, console=console, usage_logger=usage_logger,
        llm_trace=llm_trace,
    )
    if outcome.status != "Resolved":
        return "Blocked", outcome.reason, None, outcome.tokens_used, outcome.llm_model

    try:
        if sub.action == "type":
            if sub.literal_value is not None:
                # "Repeat with invalid email 'abc'" supplies the value
                # directly in the step text -- skip the Test Data lookup
                # entirely rather than searching for a label that was never
                # meant to exist for this synthetic re-run.
                value, value_detail = sub.literal_value, "literal value from step text"
            else:
                lookup_target = f"{sub.target} {sub.value_hint}" if sub.value_hint else sub.target
                value, value_detail = _extract_typed_value(lookup_target, row.test_data, run_id=run_id)
            if value is None:
                return "Blocked", f"resolved element but {value_detail}", outcome.element_source, outcome.tokens_used, outcome.llm_model
            console.action(row.test_case_id, sub.step_no, "type", outcome.selector)
            result = verbs.type_text(page, outcome.selector, value, action_timeout_ms)
        elif sub.action in ("click", "navigate"):
            console.action(row.test_case_id, sub.step_no, "click", outcome.selector)
            result = verbs.click(page, outcome.selector, action_timeout_ms)
            # Confirmed live (automationexercise.com): this ad can hijack the
            # very click meant to navigate somewhere else, landing on the
            # SAME page with only '#google_vignette' appended to the URL
            # instead -- the click itself still reports success (it
            # genuinely did something, just not the intended navigation), so
            # nothing upstream of this point has any reason to treat it as a
            # failure. clear_overlays_before_interaction already dismisses
            # the ad on the *next* step, but by then the intended navigation
            # has already been skipped for this one. Dismissing it
            # immediately and retrying the SAME click recovers the
            # navigation it interrupted, rather than silently continuing on
            # the wrong page. A bounded retry loop, not a single retry:
            # confirmed live that the same ad can reassert itself again on
            # the very next click too, so one retry isn't always enough.
            max_vignette_retries = 2
            for _ in range(max_vignette_retries):
                if "#google_vignette" not in page.url:
                    break
                if _collapse_target_already_expanded(page, outcome.selector):
                    # A Bootstrap data-toggle="collapse" element (e.g. this
                    # site's category accordion) self-heals INSIDE the
                    # verbs.click() call that just ran -- see
                    # SeleniumLocator._click_and_verify_effect's own
                    # JS-dispatched retry, which bypasses the ad overlay
                    # entirely and can already have opened the panel even
                    # though the ad's '#google_vignette' URL artifact is
                    # still sitting there. Confirmed live: the panel can
                    # already be expanded at this exact point. Clicking this
                    # SAME toggle again would just close it right back --
                    # only the stale URL needs cleaning up here, not another
                    # click.
                    clear_overlays_before_interaction(page, action_timeout_ms)
                    break
                console.line(
                    f"  [{row.test_case_id}][step {sub.step_no}] ad interstitial intercepted the "
                    f"click (URL now has #google_vignette) -- dismissing it and retrying the click"
                )
                clear_overlays_before_interaction(page, action_timeout_ms)
                result = verbs.click(page, outcome.selector, action_timeout_ms)
        elif sub.action == "hover":
            console.action(row.test_case_id, sub.step_no, "hover", outcome.selector)
            result = verbs.hover(page, outcome.selector, action_timeout_ms)
        elif sub.action == "select":
            value, value_detail = _extract_typed_value(sub.target, row.test_data)
            if value is None:
                # "Select 'Price (low to high)' from the sort dropdown" names
                # the option directly in the step text itself -- there is no
                # Test Data label to look up because the step was never
                # written as a placeholder in the first place (unlike "Select
                # the DOB month", where Test Data supplies which month). Only
                # used once a Test Data lookup has already failed, so a step
                # that genuinely does rely on Test Data still reports the
                # real "no matching Test Data label" reason instead of
                # silently typing its own unresolved target text.
                if sub.target.strip():
                    value, value_detail = sub.target.strip(), "used literal option name from step text"
                else:
                    return "Blocked", f"resolved element but {value_detail}", outcome.element_source, outcome.tokens_used, outcome.llm_model
            console.action(row.test_case_id, sub.step_no, "select", outcome.selector)
            result = verbs.select_option(page, outcome.selector, value, action_timeout_ms)
        elif sub.action == "check":
            console.action(row.test_case_id, sub.step_no, "check", outcome.selector)
            result = verbs.check(page, outcome.selector, action_timeout_ms)
        elif sub.action == "upload":
            value, value_detail = _extract_typed_value(sub.target, row.test_data)
            if value is None:
                return "Blocked", f"resolved element but {value_detail}", outcome.element_source, outcome.tokens_used, outcome.llm_model
            # Test Data often annotates a size/type hint after the filename,
            # e.g. "sample.txt (<1 MB)" -- that's documentation for a human
            # tester, not part of the filename.
            filename = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
            file_path = Path("test_data_files") / filename
            if not file_path.is_file():
                return (
                    "Blocked",
                    f"resolved element but upload file not found: {file_path} "
                    "(place the file the Test Data references under test_data_files/)",
                    outcome.element_source, outcome.tokens_used, outcome.llm_model,
                )
            console.action(row.test_case_id, sub.step_no, "upload", outcome.selector)
            result = verbs.upload(page, outcome.selector, str(file_path.resolve()), action_timeout_ms)
        elif sub.action == "accept_dialog":
            result = verbs.accept_dialog(page)
        else:
            return "Blocked", f"unhandled action '{sub.action}'", outcome.element_source, outcome.tokens_used, outcome.llm_model
    except UnexpectedAlertPresentException as exc:
        # With unhandledPromptBehavior="ignore", a click whose own JS handler
        # opens a native alert/confirm/prompt makes the click *command
        # itself* raise this -- even though the click genuinely happened and
        # produced exactly the dialog this step's own "...and accept the
        # browser alert" wording expects. Only trust that reading when the
        # step says so; an alert appearing from any other action is still a
        # real failure, not a disguised success.
        if sub.action not in ("click", "navigate") or not sub.dialog_after:
            return "Fail", f"action '{sub.action}' raised: {exc}", outcome.element_source, outcome.tokens_used, outcome.llm_model
        result = verbs.ActionResult(True, f"clicked {outcome.selector} (opened the expected browser alert)")
    except Exception as exc:  # Playwright action raised (timeout, detached, etc.)
        return "Fail", f"action '{sub.action}' raised: {exc}", outcome.element_source, outcome.tokens_used, outcome.llm_model

    status = "Pass" if result.ok else "Fail"
    detail = result.detail
    if status == "Pass" and sub.dialog_after:
        dialog_result = verbs.accept_dialog(page, action_timeout_ms)
        detail = f"{detail} | {dialog_result.detail}"
        if not dialog_result.ok:
            status = "Fail"

    # A click that deletes/deactivates/closes the account is deliberately
    # NOT invalidating the cached credentials here: a later row's genuine
    # auto_login now self-heals a stale account via the registration
    # fallback (see _run_auto_login) instead of needing the cache cleared in
    # advance, and a same-row "try logging in again with the same
    # credentials" step (attempt_login) specifically needs those now-invalid
    # credentials to still be available to actually re-attempt.

    return status, detail, outcome.element_source, outcome.tokens_used, outcome.llm_model


#  Registration is typically one row ("Register a new user") while later rows
# in the same run (login, logout, delete-account) only reference the account
# it created -- they carry no "Name:" of their own. This lets a later row's
# Expected Result still resolve "<username>" to the name whichever earlier
# row in *this run* actually registered it, without inventing a value: it is
# still a deterministic lookup of data the test author wrote down, just
# remembered across rows the same way the live browser session already
# carries the resulting login across them.
_REGISTERED_NAME_BY_RUN: dict[str, str] = {}


def _substitute_expected_result_placeholders(
    expected_result: str, test_data: str, run_id: str
) -> str:
    """Expected Result may reference a value supplied via Test Data using the
    same bracket-placeholder convention Test Data itself uses ("header shows
    'Logged in as <username>'"). This is a deterministic lookup, never an
    invented or LLM-judged value, so it stays within the spec's "parse
    Expected Result into a deterministic check" contract.
    """
    name_match = re.search(r"(?im)^\s*name\s*:\s*(.+)$", test_data)
    if name_match:
        _REGISTERED_NAME_BY_RUN[run_id] = name_match.group(1).strip()
    name = _REGISTERED_NAME_BY_RUN.get(run_id)
    if name:
        expected_result = re.sub(r"(?i)<username>", name, expected_result)
    return expected_result


_PROGRESS_PROBE_TIMEOUT_MS = 300


def _probe_expected_checks_progress(checks: list, satisfied: list[bool], page: Page) -> None:
    """Quick, short-timeout peek at each not-yet-satisfied check: mark it True
    if it already holds right now. Called after every sub-step so a check
    that is only briefly true -- e.g. a confirmation message on a page a
    later cleanup step (like "Click 'Home'") navigates away from -- still
    gets credit, instead of only ever being checked once at row-end after
    it's already gone. 300ms is enough to catch "is this already on screen"
    without meaningfully slowing down every step with a long wait for
    something that usually isn't there yet.
    """
    for i, check in enumerate(checks):
        if satisfied[i]:
            continue
        if check.kind == "text_visible":
            result = verbs.assert_text_visible(page, check.value, _PROGRESS_PROBE_TIMEOUT_MS)
        elif check.kind == "url_contains":
            result = verbs.assert_url_contains(page, check.value, _PROGRESS_PROBE_TIMEOUT_MS)
        else:
            continue
        if result.ok:
            satisfied[i] = True


def _evaluate_expected_result(
    row: TestCaseRow, page: Page, timeout_ms: int, run_id: str, pre_satisfied: list[bool] | None = None
) -> tuple[str, str]:
    expected_result = _substitute_expected_result_placeholders(row.expected_result, row.test_data, run_id)
    checks = parse_expected_result(expected_result)
    pre_satisfied = pre_satisfied or [False] * len(checks)
    unverifiable = []
    satisfied_earlier = []
    for i, check in enumerate(checks):
        if pre_satisfied[i]:
            # Already confirmed true earlier in the row (see
            # _probe_expected_checks_progress) -- do not re-check it against
            # whatever page we're on now, which may no longer show it.
            satisfied_earlier.append(check.raw_clause)
            continue
        if check.kind == "text_visible":
            result = verbs.assert_text_visible(page, check.value, timeout_ms)
            if not result.ok:
                return "Fail", f"expected check failed: {result.detail}"
        elif check.kind == "url_contains":
            result = verbs.assert_url_contains(page, check.value, timeout_ms)
            if not result.ok:
                return "Fail", f"expected check failed: {result.detail}"
        else:
            unverifiable.append(check.raw_clause)

    detail_bits = ["all deterministic checks passed"]
    if satisfied_earlier:
        detail_bits.append(f"confirmed true earlier in the row: {satisfied_earlier}")
    if unverifiable:
        detail_bits.append(f"unverifiable clauses (not auto-checked): {unverifiable}")
    return "Pass", "; ".join(detail_bits)
