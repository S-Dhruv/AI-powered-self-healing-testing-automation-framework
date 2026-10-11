"""Orchestrates the fixed resolution tier order: cache -> fallback -> AI ->
store/heal -> refuse. Never skips ahead to the LLM."""

from __future__ import annotations

from dataclasses import dataclass

from framework.browser import SeleniumPage as Page
from framework.llm.client import LlmClient
from framework.llm.usage_logger import UsageLogger
from framework.reporting.console import ConsoleNarrator
from framework.resolver.ai_finder import strategies_for_locator, try_ai_finder
from framework.resolver.cache import TierResult, try_cache
from framework.resolver.fallback import try_fallback
from framework.resolver.fingerprint import build_fingerprint
from framework.resolver.verifier import ActionKind, verify_locator
from framework.store.backend import ElementStore
from framework.store.models import Strategy
from framework.store.review import ReviewQueue

#: Every Blocked reason is tagged with one of these up front, so a reader
#: can tell "is this worth just hitting rerun, or do I need to go edit the
#: Test Step wording" at a glance, without reading the full explanation --
#: the two most common questions in practice (confirmed by what actually
#: turned out to be transient vs. a genuine wording gap across this
#: project's own test suites).
_RETRY = "RETRY"  # transient: an API/service/timing problem, not the page or the wording
_REWORD = "REWORD TEST STEP"  # structural: the step/test-data genuinely can't disambiguate the target
_RETRY_OR_REWORD = "RETRY, THEN REWORD IF IT RECURS"  # could be either; cheap to just try a rerun first
_REVIEW_QUEUE = "REVIEW QUEUE"  # needs a human approve/reject decision, not a rerun or a wording change
_CONFIG = "CONFIG"  # needs an environment/setup change before any rerun can help


def _blocked(category: str, reason: str, guidance: str) -> str:
    """Prefixes a Blocked reason with a scannable category tag, then the
    same detailed "what to do" explanation as before.

    A Blocked status always means a human needs to do something -- resolve
    an ambiguity, fix an API key, review a pending heal -- so the reason
    string should say what, not just report the raw failure. This string
    flows unchanged into the console, the Excel Actual Result cell and the
    dashboard's flip-card back face, so one edit here covers all three.
    """
    return f"[{category}] {reason} -- What to do: {guidance}"


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
        reason_text = _blocked(
            _CONFIG,
            "LLM unavailable and local tiers failed",
            "configure an LLM API key/model for this run, or add this element to the app's "
            "deterministic fallback config so it never needs the AI tier.",
        )
        console.blocked(test_case_id, step_no, reason_text)
        return ResolutionOutcome("Blocked", None, "None", 0, None, reason_text)

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
            # Authentication, quota and permission errors cannot be repaired
            # by corrective prompt feedback. Retrying them doubled token/API
            # usage and caused every later test case to be blocked as well.
            fatal_llm_error = any(
                marker in ai_result.proposal.reason.lower()
                for marker in ("401", "403", "429", "insufficient_quota", "no credits", "expired", "invalidated")
            )
            if attempt < max_attempts and not fatal_llm_error:
                console.tier_attempt(test_case_id, step_no, "3 (AI)", f"attempt {attempt} errored, retrying: {ai_result.proposal.reason}")
                retry_feedback = f"the previous call errored: {ai_result.proposal.reason}"
                continue
            reason_text = _blocked(
                _RETRY,
                f"LLM call failed: {ai_result.proposal.reason}",
                "this is an AI-service problem, not a page problem (e.g. API key, billing/quota, "
                "rate limit or network). Check the LLM usage log and the provider's dashboard, "
                "then rerun this step once it's resolved -- no wording change will fix this.",
            )
            console.blocked(test_case_id, step_no, reason_text)
            return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, reason_text)

        selector = ai_result.proposal.selector
        selector_derivation_failed = (
            ai_result.proposal.index is not None and selector is None
        )
        low_confidence = ai_result.proposal.confidence < min_confidence
        if selector is None or low_confidence:
            if attempt < max_attempts:
                if selector_derivation_failed:
                    # The LLM picked a real element (index was valid) but it has no
                    # stable attributes and no unique scoped-parent/text selector could
                    # be built for it -- this is NOT a confidence problem, so say so.
                    console.tier_attempt(
                        test_case_id, step_no, "3 (AI)",
                        f"attempt {attempt}: element index {ai_result.proposal.index} has no "
                        "derivable unique selector, retrying with feedback",
                    )
                    retry_feedback = (
                        f"your previous choice (index {ai_result.proposal.index}) could not be turned "
                        "into a unique, stable selector -- it has no id/name/data-testid/placeholder/"
                        "aria-label, and even scoping to its parent or matching its visible text did not "
                        "uniquely identify it on the page. Pick a DIFFERENT index for the same target that "
                        "has distinguishing attributes or unique visible text, if one exists."
                    )
                else:
                    console.tier_attempt(
                        test_case_id, step_no, "3 (AI)",
                        f"attempt {attempt} low confidence ({ai_result.proposal.confidence:.2f}), retrying with feedback",
                    )
                    retry_feedback = (
                        f"confidence was too low ({ai_result.proposal.confidence:.2f}); "
                        f"reason given: {ai_result.proposal.reason}. Look again, more carefully, "
                        "at the full element list before giving up."
                    )
                continue
            short_reason = (
                f"element index {ai_result.proposal.index} had no derivable unique selector"
                if selector_derivation_failed
                else f"low confidence ({ai_result.proposal.confidence:.2f} < {min_confidence}) or no selector proposed"
            )
            if selector_derivation_failed:
                guidance = (
                    "the page has multiple structurally-identical elements for this target "
                    "(e.g. repeated hover-reveal buttons with the same text and no id/data-testid) "
                    "and none of them can be turned into a unique selector. Reword the Test Step to "
                    "add a distinguishing detail the AI can act on (nearby text, position), or ask "
                    "the app team to add a stable attribute to that element."
                )
            else:
                guidance = (
                    "the AI wasn't confident enough to proceed safely. Make the Test Step/Expected "
                    "Result description more specific (visible text, position, nearby label) and rerun; "
                    "if it keeps happening, the target may be genuinely ambiguous on this page."
                )
            category = _REWORD if selector_derivation_failed else _RETRY_OR_REWORD
            reason_text = _blocked(category, f"{short_reason} after {attempt} attempt(s): {ai_result.proposal.reason}", guidance)
            console.blocked(test_case_id, step_no, reason_text)
            return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, reason_text)

        # Tier 4: verify before use or storage
        verification = verify_locator(page, selector, action, description)
        console.verify(test_case_id, step_no, verification.ok, verification.reason)
        if verification.ok:
            break

        if attempt < max_attempts:
            console.tier_attempt(test_case_id, step_no, "3 (AI)", f"attempt {attempt} failed verification, retrying with feedback")
            retry_feedback = f"the proposed selector failed deterministic verification: {verification.reason}"
            continue

        # "matched more than one DIFFERENT element" is a real wording gap
        # (the step/test-data can't tell them apart); "not visible" or a
        # timing-shaped reason is far more often a transient ad/overlay/
        # page-settling issue that a plain rerun clears on its own.
        verification_category = (
            _REWORD if "matched multiple different visible elements" in verification.reason else _RETRY_OR_REWORD
        )
        reason_text = _blocked(
            verification_category,
            f"AI proposal failed verification after {attempt} attempt(s): {verification.reason}",
            "the AI's chosen element failed a deterministic safety check (not unique, not visible, "
            "or the wrong element type for this action) -- this is treated as a hard stop, not a "
            "heal, because an unverified locator must never be used. Check the reason above: if it "
            "says the selector matched more than one element, reword the step to disambiguate the "
            "target; if it says not visible, check the step ordering/timing.",
        )
        console.blocked(test_case_id, step_no, reason_text)
        return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, reason_text)

    # Build fingerprint from the same element the verifier validated -- or,
    # if verification redirected to a visible twin (see
    # verifier._find_visible_twin), from that one instead.
    selector = ai_result.proposal.selector
    if selector is None:
        reason_text = _blocked(
            _REWORD,
            "AI returned an index that could not be converted to a unique selector",
            "reword the Test Step to add a distinguishing detail (visible text, position, nearby "
            "label) for the AI to use, or add a stable attribute to that element in the application.",
        )
        return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, reason_text)
    selector = verification.resolved_selector or selector
    locator = page.locator(selector)
    fingerprint = build_fingerprint(locator)
    strategies = strategies_for_locator(locator, selector)

    if had_prior_record:
        # Healing: save as pending (unless auto-approve), keep old version active until approved.
        record = store.insert(
            application=application, environment=environment, page=target_page, description=description,
            strategies=strategies, fingerprint=fingerprint, status="pending", source="Healed", run_id=run_id,
        )
        min_conf_met = ai_result.proposal.confidence >= min_confidence and verification.ok
        record = review_queue.auto_approve_if_configured(record, healing_auto_approve, min_conf_met)
        console.store(test_case_id, step_no, record.version, record.status)
        element_source = "Healed"
    else:
        # First learning: store active immediately (nothing to supersede/review).
        record = store.insert(
            application=application, environment=environment, page=target_page, description=description,
            strategies=strategies, fingerprint=fingerprint, status="active", source="AI", run_id=run_id,
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
        reason_text = _blocked(
            _REVIEW_QUEUE,
            "pending review, no prior version to fall back on",
            "a healed locator is awaiting manual approval (healing.mode is not 'auto') and this is "
            "the first-ever reference for this element, so there's no earlier version to use in the "
            "meantime. Approve or reject it in the review queue, then rerun this step.",
        )
        console.blocked(test_case_id, step_no, reason_text)
        return ResolutionOutcome("Blocked", None, "None", tokens_used, llm_client.model, reason_text)

    return ResolutionOutcome("Resolved", selector, element_source, tokens_used, llm_client.model, "resolved via AI")


def _selector_for(strategy: Strategy) -> str:
    from framework.resolver.cache import _selector_for_strategy
    return _selector_for_strategy(strategy)
