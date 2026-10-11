"""Build and compare element fingerprints (spec §4.3).

Fingerprint fields: tag, role, accessible name, visible text (normalised),
stable attributes, and a coarse structural hint (parent landmark) used only as
a weak signal, never a primary match criterion.
"""

from __future__ import annotations

import re

from framework.browser import SeleniumLocator as Locator

from framework.store.models import Fingerprint

_STABLE_ATTRS = ("id", "name", "data-testid", "type", "placeholder")
_WS_RE = re.compile(r"\s+")
# Mirrors ai_finder.py's ICON_CLASS_RE: an icon-only element's only hint of
# meaning is often an icon-font class (Font Awesome "fa-...", Bootstrap
# "glyphicon-...", or a generic "icon-..."), on any site using these common
# conventions, not one site's markup.
_ICON_CLASS_RE = re.compile(r"\b(?:fa|glyphicon|icon)-([a-z0-9][a-z0-9-]*)\b", re.I)


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
    # Sites commonly repeat the same name/type/placeholder across two
    # genuinely different widgets (e.g. a login form and a signup form on the
    # same page both using name="email") and instead disambiguate them with
    # their own custom data-* attribute (data-qa, data-cy, data-test, ...).
    # Scanning all of them generically -- not any one fixed name -- is what
    # lets the "are these actually identical copies?" check below tell two
    # such lookalike elements apart on any site, not just this one.
    extra_data_attrs: dict[str, str] = locator.evaluate(
        "el => Object.fromEntries([...el.attributes]"
        ".filter(a => a.name.startsWith('data-') && a.name !== 'data-testid' && a.name !== 'data-sh-idx')"
        ".map(a => [a.name, a.value]))"
    ) or {}
    for name, val in sorted(extra_data_attrs.items()):
        if val:
            attrs[name] = val

    if not visible_text:
        own_class = locator.evaluate("el => el.className || ''") or ""
        icon_match = _ICON_CLASS_RE.search(own_class)
        if not icon_match:
            child_class = locator.evaluate(
                "el => { const c = el.querySelector('i, span'); return c ? (c.className || '') : ''; }"
            ) or ""
            icon_match = _ICON_CLASS_RE.search(child_class)
        if icon_match:
            attrs["icon_hint"] = icon_match.group(1).replace("-", " ")

    # Mirrors ai_finder.py's getNearbyText(): a control repeated once per
    # item in a listing (e.g. "Add to cart" on every card in a product grid)
    # reads identically to text-only matching -- its immediate parent's
    # residual text (the product's own name/price) is what actually
    # distinguishes it, and is what the AI finder's element list already
    # showed the model when it picked this element, so the plausibility
    # check below needs the same signal to avoid rejecting a correct answer.
    parent_text = locator.evaluate(
        "el => { const p = el.parentElement; if (!p) return null; "
        "const own = (el.innerText || el.value || '').trim(); "
        "let residual = (p.innerText || '').trim(); "
        "if (own) residual = residual.replace(own, '').trim(); "
        "return residual.replace(/\\s+/g, ' '); }"
    ) or ""
    if parent_text and len(parent_text) <= 120:
        attrs["context"] = parent_text[:80]

    # Two different <form>s (e.g. "Login to your account" vs "New User
    # Signup!") both just land on the landmark tag "form" with no further
    # detail -- include whichever of the form's own identifying attributes is
    # present so two distinct forms of the same tag aren't indistinguishable
    # from each other on sites that don't use data-* attributes at all.
    parent_landmark = locator.evaluate(
        "el => { const p = el.closest('[role], nav, header, footer, main, form'); "
        "if (!p) return null; "
        "const kind = p.getAttribute('role') || p.tagName.toLowerCase(); "
        "const ident = p.id || p.getAttribute('name') || p.getAttribute('action') || ''; "
        "return ident ? `${kind}:${ident}` : kind; }"
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
