"""Build and compare element fingerprints (spec §4.3).

Fingerprint fields: tag, role, accessible name, visible text (normalised),
stable attributes, and a coarse structural hint (parent landmark) used only as
a weak signal, never a primary match criterion.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator

from framework.store.models import Fingerprint

_STABLE_ATTRS = ("id", "name", "data-testid", "type", "placeholder")
_WS_RE = re.compile(r"\s+")


def _normalise(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = _WS_RE.sub(" ", text).strip()
    return collapsed or None


def build_fingerprint(locator: Locator) -> Fingerprint:
    """Build a Fingerprint from a Playwright locator that currently matches exactly one element."""
    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    role = locator.evaluate(
        "el => el.getAttribute('role') || null"
    )
    accessible_name = locator.evaluate(
        "el => (el.getAttribute('aria-label') || el.innerText || el.value || '').trim() || null"
    )
    visible_text = _normalise(locator.evaluate("el => el.innerText || ''"))

    attrs: dict[str, str] = {}
    for attr in _STABLE_ATTRS:
        val = locator.evaluate(f"el => el.getAttribute('{attr}')")
        if val:
            attrs[attr] = val
    href = locator.evaluate("el => el.getAttribute('href')")
    if href:
        attrs["href_pattern"] = re.sub(r"\d+", "#", href)

    parent_landmark = locator.evaluate(
        "el => { const p = el.closest('[role], nav, header, footer, main, form'); "
        "return p ? (p.getAttribute('role') || p.tagName.toLowerCase()) : null; }"
    )

    return Fingerprint(
        tag=tag,
        role=role,
        accessible_name=_normalise(accessible_name),
        visible_text=visible_text,
        attributes=attrs,
        parent_landmark=parent_landmark,
    )


def fingerprint_similarity(a: Fingerprint, b: Fingerprint) -> float:
    """Weighted similarity in [0.0, 1.0]. Position/size is intentionally excluded
    as a primary criterion — only the stable identity fields count here."""
    score = 0.0
    weights_total = 0.0

    def add(weight: float, equal: bool) -> None:
        nonlocal score, weights_total
        weights_total += weight
        if equal:
            score += weight

    add(0.20, a.tag == b.tag)
    add(0.20, (a.role or "") == (b.role or ""))
    add(0.25, (a.accessible_name or "") == (b.accessible_name or ""))
    add(0.15, (a.visible_text or "") == (b.visible_text or ""))
    add(0.10, a.attributes.get("id") == b.attributes.get("id"))
    add(0.05, a.attributes.get("data-testid") == b.attributes.get("data-testid"))
    add(0.05, (a.parent_landmark or "") == (b.parent_landmark or ""))

    return score / weights_total if weights_total else 0.0
