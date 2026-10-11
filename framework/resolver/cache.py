"""Tier 1: cache lookup. Resolve the primary stored locator; accept only if it
matches exactly one visible, action-compatible, fingerprint-consistent element."""

from __future__ import annotations

from dataclasses import dataclass

from framework.browser import SeleniumPage as Page

from framework.resolver.verifier import ActionKind, VerificationResult, verify_locator
from framework.store.backend import ElementStore
from framework.store.models import ElementRecord, Strategy


@dataclass
class TierResult:
    tier: str
    hit: bool
    selector: str | None
    strategy_kind: str | None
    record: ElementRecord | None
    verification: VerificationResult | None
    reason: str


def _selector_for_strategy(strategy: Strategy) -> str:
    if strategy.kind == "test_id":
        return f"[data-testid='{strategy.value}']"
    if strategy.kind == "role_name" and strategy.role:
        return f"role={strategy.role}[name='{strategy.value}']"
    if strategy.kind == "label":
        return f"label:has-text('{strategy.value}') >> input, text='{strategy.value}'"
    if strategy.kind == "text":
        return f"text='{strategy.value}'"
    return strategy.value  # css


def try_cache(
    store: ElementStore,
    page: Page,
    *,
    application: str,
    environment: str,
    target_page: str,
    description: str,
    action: ActionKind,
    min_similarity: float,
) -> TierResult:
    record = store.get_active(application, environment, target_page, description)
    if record is None:
        return TierResult("Cache", False, None, None, None, None, "no active record in store")

    primary = record.strategies[0]
    selector = _selector_for_strategy(primary)
    verification = verify_locator(
        page, selector, action, description,
        expected_fingerprint=record.fingerprint, min_similarity=min_similarity,
    )
    if verification.ok:
        store.record_hit(record.id)
        used_selector = verification.resolved_selector or selector
        return TierResult("Cache", True, used_selector, primary.kind, record, verification, "cache hit")

    store.record_miss(record.id)
    return TierResult(
        "Cache", False, selector, primary.kind, record, verification,
        f"primary strategy failed verification: {verification.reason}",
    )
