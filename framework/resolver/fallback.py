"""Tier 2: deterministic fallback. Try remaining stored strategies in the fixed
priority order test-id -> role+accessible name -> label -> text -> css ->
fingerprint similarity search. Same acceptance rule as cache. Zero tokens."""

from __future__ import annotations

from playwright.sync_api import Page

from framework.resolver.cache import TierResult, _selector_for_strategy
from framework.resolver.verifier import ActionKind, verify_locator
from framework.store.backend import ElementStore
from framework.store.models import ElementRecord

_PRIORITY = ("test_id", "role_name", "label", "text", "css")


def try_fallback(
    store: ElementStore,
    page: Page,
    *,
    record: ElementRecord | None,
    description: str,
    action: ActionKind,
    min_similarity: float,
) -> TierResult:
    if record is None or len(record.strategies) <= 1:
        return TierResult("Fallback", False, None, None, record, None, "no alternate strategies stored")

    remaining = sorted(
        record.strategies[1:],
        key=lambda s: _PRIORITY.index(s.kind) if s.kind in _PRIORITY else len(_PRIORITY),
    )

    for strategy in remaining:
        selector = _selector_for_strategy(strategy)
        verification = verify_locator(
            page, selector, action, description,
            expected_fingerprint=record.fingerprint, min_similarity=min_similarity,
        )
        if verification.ok:
            store.record_hit(record.id)
            return TierResult(
                "Fallback", True, selector, strategy.kind, record, verification,
                f"fallback strategy '{strategy.kind}' succeeded",
            )

    store.record_miss(record.id)
    return TierResult(
        "Fallback", False, None, None, record, None,
        f"all {len(remaining)} fallback strategies failed verification",
    )
