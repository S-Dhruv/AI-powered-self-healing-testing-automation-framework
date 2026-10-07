"""Orchestrates the fixed resolution tier order: cache -> fallback -> AI ->
store/heal -> refuse. Never skips ahead to the LLM."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from framework.llm.client import LlmClient
from framework.llm.usage_logger import UsageLogger
from framework.resolver.ai_finder import try_ai_finder
from framework.resolver.cache import TierResult, try_cache
from framework.resolver.fallback import try_fallback
from framework.resolver.fingerprint import build_fingerprint
from framework.resolver.verifier import ActionKind, verify_locator
from framework.reporting.console import ConsoleNarrator
from framework.store.backend import ElementStore
from framework.store.models import Strategy
from framework.store.review import ReviewQueue


@dataclass
class ResolutionOutcome:
    status: str  # "Resolved" | "Blocked"
    selector: str | None
    element_source: str  # "Cache" | "Fallback" | "AI" | "Healed"
    tokens_used: int
    llm_model: str | None
    reason: str


def resolve_element(
    *,
    store: ElementStore,
    review_queue: ReviewQueue,
    llm_client: LlmClient | None,
    page: Page,
    application: str,
    environment: str,
    target_page: str,
    description: str,
    action: ActionKind,
    run_id: str,
    test_case_id: str,
    step_no: int,
    user_id: str,
    min_similarity: float,
    min_confidence: float,
    healing_auto_approve: bool,
    console: ConsoleNarrator,
    usage_logger: UsageLogger | None = None,
    llm_trace: bool = False,
) -> ResolutionOutcome:
    # Tier 1: cache
    cache_result = try_cache(
        store, page, application=application, environment=environment, target_page=target_page,
        description=description, action=action, min_similarity=min_similarity,
    )
    console.tier_attempt(test_case_id, step_no, "1 (Cache)", "HIT" if cache_result.hit else f"MISS ({cache_result.reason})")
    if cache_result.hit:
        return ResolutionOutcome("Resolved", cache_result.selector, "Cache", 0, None, cache_result.reason)

    # Tier 2: deterministic fallback
    fallback_result = try_fallback(
        store, page, record=cache_result.record, description=description,
        action=action, min_similarity=min_similarity,
    )
    console.tier_attempt(test_case_id, step_no, "2 (Fallback)", "HIT" if fallback_result.hit else f"MISS ({fallback_result.reason})")
    if fallback_result.hit:
        return ResolutionOutcome("Resolved", fallback_result.selector, "Fallback", 0, None, fallback_result.reason)

    # "Previously existed" (spec §4.4) means any version in history, not just an
    # active one — an invalidated/superseded record still means this is a heal,
    # not a first learning.
    had_prior_record = len(store.history(application, environment, target_page, description)) > 0

    # Tier 3: AI identification
    if llm_client is None:
        console.blocked(test_case_id, step_no, "no LLM client configured; refusing to guess")
        return ResolutionOutcome("Blocked", None, "None", 0, None, "LLM unavailable and local tiers failed")

    reason = "healing" if had_prior_record else "first_learning"
    tokens_used = 0
    retry_feedback: str | None = None
    verification = None
    max_attempts = 2  # one original attempt + one retry with corrective feedback

    for attempt in range(1, max_attempts + 1):
        ai_result = try_ai_finder(
            llm_client, page, target_description=description, action=action, run_id=run_id,
            test_case_id=test_case_id, step_no=step_no, user_id=user_id, reason=reason,
            retry_feedback=retry_feedback,
        )
        console.llm_call(
            test_case_id, step_no, llm_client.model,
            ai_result.proposal.input_tokens, ai_result.proposal.output_tokens, ai_result.proposal.confidence,
        )
        if llm_trace:
            console.llm_io(test_case_id, step_no, ai_result.proposal.user_prompt, ai_result.proposal.raw_text)
        if usage_logger is not None:
            usage_logger.log(ai_result.usage_entry)
        tokens_used += ai_result.proposal.input_tokens + ai_result.proposal.output_tokens

        if ai_result.proposal.status == "error":
            if attempt < max_attempts:
                console.tier_attempt(test_case_id, step_no, "3 (AI)", f"attempt {attempt} errored, retrying: {ai_result.proposal.reason}")
                retry_feedback = f"the previous call errored: {ai_result.proposal.reason}"
                continue
            console.blocked(test_case_id, step_no, f"LLM call failed: {ai_result.proposal.reason}")
            return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, ai_result.proposal.reason)

        if ai_result.proposal.selector is None or ai_result.proposal.confidence < min_confidence:
            if attempt < max_attempts:
                console.tier_attempt(
                    test_case_id, step_no, "3 (AI)",
                    f"attempt {attempt} low confidence ({ai_result.proposal.confidence:.2f}), retrying with feedback",
                )
                retry_feedback = (
                    f"confidence was too low ({ai_result.proposal.confidence:.2f}) or no selector was "
                    f"proposed; reason given: {ai_result.proposal.reason}. Look again, more carefully, "
                    "at the full element list before giving up."
                )
                continue
            console.blocked(
                test_case_id, step_no,
                f"low confidence ({ai_result.proposal.confidence:.2f} < {min_confidence}) or no selector proposed",
            )
            return ResolutionOutcome(
                "Blocked", None, "None", tokens_used, llm_client.model,
                f"low confidence or no proposal after {attempt} attempt(s): {ai_result.proposal.reason}",
            )

        # Tier 4: verify before use or storage
        verification = verify_locator(page, ai_result.proposal.selector, action, description)
        console.verify(test_case_id, step_no, verification.ok, verification.reason)
        if verification.ok:
            break

        if attempt < max_attempts:
            console.tier_attempt(test_case_id, step_no, "3 (AI)", f"attempt {attempt} failed verification, retrying with feedback")
            retry_feedback = f"the proposed selector failed deterministic verification: {verification.reason}"
            continue

        console.blocked(test_case_id, step_no, f"AI proposal failed verification: {verification.reason}")
        return ResolutionOutcome(
            "Blocked", None, "None", tokens_used, llm_client.model,
            f"AI proposal failed verification after {attempt} attempt(s): {verification.reason}",
        )

    # Build fingerprint from the same element the verifier validated
    locator = page.locator(ai_result.proposal.selector)
    if verification.used_first_match:
        locator = locator.first
    fingerprint = build_fingerprint(locator)
    # Stored as kind="css": the LLM returns a ready-to-use Playwright selector
    # string already; re-wrapping it under its reported strategy kind (e.g.
    # test_id) would double-wrap it (e.g. [data-testid='[data-testid=...]']).
    # proposal.strategy is kept only in the log/reason for human diagnosis.
    strategy = Strategy(kind="css", value=ai_result.proposal.selector)

    if had_prior_record:
        # Healing: save as pending (unless auto-approve), keep old version active until approved.
        record = store.insert(
            application=application, environment=environment, page=target_page, description=description,
            strategies=[strategy], fingerprint=fingerprint, status="pending", source="Healed", run_id=run_id,
        )
        min_conf_met = ai_result.proposal.confidence >= min_confidence and verification.ok
        record = review_queue.auto_approve_if_configured(record, healing_auto_approve, min_conf_met)
        console.store(test_case_id, step_no, record.version, record.status)
        element_source = "Healed"
    else:
        # First learning: store active immediately (nothing to supersede/review).
        record = store.insert(
            application=application, environment=environment, page=target_page, description=description,
            strategies=[strategy], fingerprint=fingerprint, status="active", source="AI", run_id=run_id,
        )
        console.store(test_case_id, step_no, record.version, record.status)
        element_source = "AI"

    if record.status != "active":
        # Heal pending manual review: old reference stays active and usable now.
        if had_prior_record and cache_result.record is not None:
            return ResolutionOutcome(
                "Resolved", _selector_for(cache_result.record.strategies[0]), "Cache",
                tokens_used, llm_client.model, "heal pending review; using previous active version",
            )
        console.blocked(test_case_id, step_no, "healed reference pending review and no prior usable version")
        return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, "pending review, no prior version to fall back on")

    return ResolutionOutcome("Resolved", ai_result.proposal.selector, element_source, tokens_used, llm_client.model, "resolved via AI")


def _selector_for(strategy: Strategy) -> str:
    from framework.resolver.cache import _selector_for_strategy
    return _selector_for_strategy(strategy)
