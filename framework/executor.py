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

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.sync_api import Page

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
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue

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


def resolve_navigate_target(step_target: str, base_url: str) -> str:
    """Documented assumption: an explicit URL in the step text is used as-is;
    an unqualified target ("home page", "the site", etc.) has no URL to
    extract, so it resolves to this environment's configured base_url rather
    than being passed through as a literal (non-URL) string."""
    return step_target if re.match(r"^https?://", step_target) else base_url


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


def _extract_typed_value(target: str, test_data: str) -> tuple[str | None, str]:
    lines = [ln.strip() for ln in test_data.splitlines() if ln.strip()]
    if len(lines) == 1 and ":" not in lines[0]:
        return lines[0], "used sole Test Data line"
    target_words = {w.lower() for w in re.findall(r"[A-Za-z]+", target)}
    for line in lines:
        m = re.match(r"^([A-Za-z0-9 _/-]+):\s*(.+)$", line)
        if not m:
            continue
        label_words = {w.lower() for w in re.findall(r"[A-Za-z]+", m.group(1))}
        if target_words & label_words:
            return m.group(2).strip(), f"matched Test Data label '{m.group(1).strip()}'"
    return None, "no matching Test Data label found"


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
) -> RowExecutionResult:
    started = time.monotonic()
    sub_steps = parse_test_steps(row.test_step)
    console.step_start(row.test_case_id, 0, "test case", row.title)

    details: list[str] = []
    worst_status = "Pass"
    element_sources: set[str] = set()
    tokens_total = 0
    llm_model: str | None = None

    for sub in sub_steps:
        outcome_status, outcome_detail, source, tokens, model = _run_sub_step(
            row, sub, page=page, store=store, review_queue=review_queue, llm_client=llm_client,
            allow_guard=allow_guard, application=application, environment=environment,
            page_name=page_name, base_url=base_url, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
            min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
            action_timeout_ms=action_timeout_ms, navigation_timeout_ms=navigation_timeout_ms,
            console=console, usage_logger=usage_logger, llm_trace=llm_trace,
        )
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
        exp_status, exp_detail = _evaluate_expected_result(row, page, action_timeout_ms)
        details.append(exp_detail)
        if _STATUS_RANK.get(exp_status, 0) > _STATUS_RANK[worst_status]:
            worst_status = exp_status

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

    for field_target in field_targets:
        value, value_detail = _extract_typed_value(field_target, row.test_data)
        if value is None:
            details.append(f"{field_target}: Blocked - {value_detail}")
            worst_status = _worse(worst_status, "Blocked")
            continue

        field_outcome = resolve_element(
            store=store, review_queue=review_queue, llm_client=llm_client, page=page,
            application=application, environment=environment, target_page=page_name,
            description=field_target, action="type", run_id=run_id, test_case_id=row.test_case_id,
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

        try:
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


def _run_sub_step(
    row: TestCaseRow, sub: SubStep, *, page, store, review_queue, llm_client, allow_guard,
    application, environment, page_name, base_url, run_id, user_id, min_similarity, min_confidence,
    healing_auto_approve, action_timeout_ms, navigation_timeout_ms, console, usage_logger=None,
    llm_trace=False,
) -> tuple[str, str, str | None, int, str | None]:
    if sub.action == "noop":
        return "Pass", "no-op step, nothing to do", None, 0, None

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
        result = verbs.navigate(page, target_url, allow_guard, navigation_timeout_ms)
        if result.ok:
            # Dismiss any ad overlays that may have appeared on the new page
            clear_overlays_before_interaction(page, action_timeout_ms)
        return ("Pass" if result.ok else "Blocked"), result.detail, None, 0, None

    # Before attempting any element interaction, dismiss any ad overlays
    clear_overlays_before_interaction(page, action_timeout_ms)

    resolver_action_map = {
        "click": "click", "type": "type", "select": "select", "check": "check",
        "upload": "click", "accept_dialog": "click", "assert_visible": "assert_visible",
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
        if len(field_targets) > 1:
            return _run_multi_field_type(
                row, sub, field_targets, page=page, store=store, review_queue=review_queue,
                llm_client=llm_client, application=application, environment=environment,
                page_name=page_name, run_id=run_id, user_id=user_id, min_similarity=min_similarity,
                min_confidence=min_confidence, healing_auto_approve=healing_auto_approve,
                action_timeout_ms=action_timeout_ms, console=console, usage_logger=usage_logger,
                llm_trace=llm_trace,
            )

    outcome = resolve_element(
        store=store, review_queue=review_queue, llm_client=llm_client, page=page,
        application=application, environment=environment, target_page=page_name,
        description=sub.target, action=resolver_action, run_id=run_id,
        test_case_id=row.test_case_id, step_no=sub.step_no, user_id=user_id,
        min_similarity=min_similarity, min_confidence=min_confidence,
        healing_auto_approve=healing_auto_approve, console=console, usage_logger=usage_logger,
        llm_trace=llm_trace,
    )
    if outcome.status != "Resolved":
        return "Blocked", outcome.reason, None, outcome.tokens_used, outcome.llm_model

    try:
        if sub.action == "type":
            value, value_detail = _extract_typed_value(sub.target, row.test_data)
            if value is None:
                return "Blocked", f"resolved element but {value_detail}", outcome.element_source, outcome.tokens_used, outcome.llm_model
            result = verbs.type_text(page, outcome.selector, value, action_timeout_ms)
        elif sub.action == "click":
            result = verbs.click(page, outcome.selector, action_timeout_ms)
        elif sub.action == "select":
            value, value_detail = _extract_typed_value(sub.target, row.test_data)
            if value is None:
                return "Blocked", f"resolved element but {value_detail}", outcome.element_source, outcome.tokens_used, outcome.llm_model
            result = verbs.select_option(page, outcome.selector, value, action_timeout_ms)
        elif sub.action == "check":
            result = verbs.check(page, outcome.selector, action_timeout_ms)
        elif sub.action == "upload":
            return "Blocked", "file upload requires an explicit file path; not provided in Test Data grammar", outcome.element_source, outcome.tokens_used, outcome.llm_model
        elif sub.action == "accept_dialog":
            result = verbs.accept_dialog(page)
        else:
            return "Blocked", f"unhandled action '{sub.action}'", outcome.element_source, outcome.tokens_used, outcome.llm_model
    except Exception as exc:  # Playwright action raised (timeout, detached, etc.)
        return "Fail", f"action '{sub.action}' raised: {exc}", outcome.element_source, outcome.tokens_used, outcome.llm_model

    status = "Pass" if result.ok else "Fail"
    return status, result.detail, outcome.element_source, outcome.tokens_used, outcome.llm_model


def _evaluate_expected_result(row: TestCaseRow, page: Page, timeout_ms: int) -> tuple[str, str]:
    checks = parse_expected_result(row.expected_result)
    unverifiable = []
    for check in checks:
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

    if unverifiable:
        return "Pass", f"all deterministic checks passed; unverifiable clauses (not auto-checked): {unverifiable}"
    return "Pass", "all expected-result checks passed"
