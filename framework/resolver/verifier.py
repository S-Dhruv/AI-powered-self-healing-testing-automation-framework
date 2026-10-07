"""Deterministic verification gate (spec §4.2). Mandatory before any locator is
used or stored — never interact with, or persist, an unverified locator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Locator, Page

from framework.resolver.fingerprint import build_fingerprint, fingerprint_similarity
from framework.store.models import Fingerprint

ActionKind = Literal[
    "navigate", "click", "type", "select", "check", "assert_text", "assert_visible", "assert_url"
]

_ACTION_COMPATIBLE_TAGS: dict[ActionKind, set[str]] = {
    "click": {"a", "button", "input", "div", "span", "li", "label"},
    "type": {"input", "textarea"},
    "select": {"select"},
    "check": {"input"},
}


@dataclass
class VerificationResult:
    ok: bool
    reason: str
    fingerprint: Fingerprint | None = None
    similarity: float | None = None
    used_first_match: bool = False  # True if selector matched 2+ elements and we verified the first


def verify_locator(
    page: Page,
    selector: str,
    action: ActionKind,
    target_description: str,
    expected_fingerprint: Fingerprint | None = None,
    min_similarity: float = 0.80,
) -> VerificationResult:
    """All checks must pass: at least one match, visible, action-compatible,
    plausibly consistent with the description, and fingerprint-consistent
    if a prior fingerprint is supplied. When multiple matches found, uses the first."""
    locator: Locator = page.locator(selector)
    used_first_match = False

    count = locator.count()
    if count == 0:
        return VerificationResult(False, f"selector matched 0 elements, expected at least 1")
    if count > 1:
        # Multiple matches: use the first one
        locator = locator.first
        used_first_match = True

    if action in ("click", "type", "select", "check"):
        if not locator.is_visible():
            return VerificationResult(False, "element is not visible")
        if not locator.is_enabled():
            return VerificationResult(False, "element is not enabled")

    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    if action in _ACTION_COMPATIBLE_TAGS and tag not in _ACTION_COMPATIBLE_TAGS[action]:
        return VerificationResult(
            False, f"element <{tag}> is not compatible with action '{action}'"
        )

    fingerprint = build_fingerprint(locator)

    if not _plausible_match(fingerprint, target_description):
        return VerificationResult(
            False,
            f"element (role={fingerprint.role}, name={fingerprint.accessible_name!r}) is not "
            f"plausibly consistent with target description {target_description!r}",
            fingerprint=fingerprint,
            used_first_match=used_first_match,
        )

    if expected_fingerprint is not None:
        similarity = fingerprint_similarity(fingerprint, expected_fingerprint)
        if similarity < min_similarity:
            return VerificationResult(
                False,
                f"fingerprint similarity {similarity:.2f} below threshold {min_similarity:.2f}",
                fingerprint=fingerprint,
                similarity=similarity,
                used_first_match=used_first_match,
            )
        return VerificationResult(True, "verified", fingerprint=fingerprint, similarity=similarity, used_first_match=used_first_match)

    return VerificationResult(True, "verified", fingerprint=fingerprint, used_first_match=used_first_match)


def _plausible_match(fingerprint: Fingerprint, target_description: str) -> bool:
    """Basic lexical/role guard against confident-but-wrong answers: at least one
    significant word from the target description must appear in the element's
    accessible name, visible text, or stable attributes."""
    words = {w.lower() for w in target_description.split() if len(w) > 2}
    if not words:
        return True
    haystack = " ".join(
        filter(
            None,
            [
                fingerprint.accessible_name,
                fingerprint.visible_text,
                fingerprint.attributes.get("placeholder"),
                fingerprint.attributes.get("name"),
                fingerprint.attributes.get("id"),
            ],
        )
    ).lower()
    return any(word in haystack for word in words)
