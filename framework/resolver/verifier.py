"""Deterministic verification gate (spec §4.2). Mandatory before any locator is
used or stored — never interact with, or persist, an unverified locator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from framework.actions.ad_utils import clear_overlays_before_interaction
from framework.browser import SeleniumLocator as Locator
from framework.browser import SeleniumPage as Page

from framework.resolver.fingerprint import build_fingerprint, fingerprint_similarity
from framework.store.models import Fingerprint
from framework.synonyms import FIELD_CONCEPT_SYNONYMS

ActionKind = Literal[
    "navigate", "click", "hover", "type", "select", "check", "assert_text", "assert_visible", "assert_url"
]

_ACTION_COMPATIBLE_TAGS: dict[ActionKind, set[str]] = {
    "click": {"a", "button", "input", "div", "span", "li", "label"},
    "type": {"input", "textarea"},
    "select": {"select"},
    "check": {"input"},
    # Deliberately no entry for "hover": unlike click/type/select/check,
    # there is no tag a hover gesture is incompatible with -- any element on
    # the page can be hovered, so this is correctly treated as unrestricted
    # by the same "if action in _ACTION_COMPATIBLE_TAGS" check used for
    # every other action kind, rather than adding a set of "all tags" here.
}


@dataclass
class VerificationResult:
    ok: bool
    reason: str
    fingerprint: Fingerprint | None = None
    similarity: float | None = None
    used_first_match: bool = False  # True if selector matched 2+ elements and we verified the first
    # Set when verification succeeded against a DIFFERENT selector than the
    # one passed in (see _find_visible_twin) -- callers must use this one
    # for every subsequent action and for whatever gets stored, not the
    # original selector string.
    resolved_selector: str | None = None


def _find_visible_twin(page: Page, locator: Locator, selector: str) -> tuple[Locator, str] | None:
    """A hover-reveal listing UI (e.g. a product card's image-overlay "Add to
    cart" sitting on top of a plainer, always-present one underneath) commonly
    renders the SAME logical control twice, with only one of the two actually
    on screen at a time -- and which one varies by page/session state, not by
    anything this framework controls. The two copies usually share no single
    CSS selector (different ancestor structure), so the ambiguity-detection
    above (one selector matching 2+ elements) never sees them as related.
    They do still share their own custom data-* attribute(s) (e.g. the same
    data-product-id) -- re-querying by just those is what ties them back
    together generically, for any site using this common pattern, not any
    one site's markup.
    """
    try:
        data_attrs: dict[str, str] = locator.evaluate(
            "el => Object.fromEntries([...el.attributes]"
            ".filter(a => a.name.startsWith('data-') && a.name !== 'data-sh-idx')"
            ".map(a => [a.name, a.value]))"
        ) or {}
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        return None
    if not data_attrs:
        return None
    attr_suffix = "".join(f'[{name}="{value}"]' for name, value in sorted(data_attrs.items()) if value)
    if not attr_suffix:
        return None
    twin_selector = f"{tag}{attr_suffix}"
    if twin_selector == selector:
        return None
    try:
        twin_locator = page.locator(twin_selector)
        if twin_locator.count() < 2:
            return None
        for candidate in twin_locator.visible_locators():
            return candidate, twin_selector
    except Exception:
        return None
    return None


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
    original_selector = selector
    # Best-effort, bounded: a stored selector/fingerprint can be entirely
    # correct and still get judged wrong here if a third-party ad overlay is
    # transiently sitting on top of the page at the exact instant this runs
    # (confirmed as a live, known instability on automationexercise.com --
    # already handled right before every *action*, see
    # clear_overlays_before_interaction's other call sites in executor.py --
    # but never before the verification check itself, which runs first and
    # can reject a perfectly valid cached element before an action ever gets
    # the chance to clear the same overlay). Never raises; a page with
    # nothing to clear just continues immediately.
    try:
        clear_overlays_before_interaction(page)
    except Exception:
        pass
    locator: Locator = page.locator(selector)
    used_first_match = False

    count = locator.count()
    if count == 0:
        return VerificationResult(False, f"selector matched 0 elements, expected at least 1")
    if count > 1:
        # Raw DOM count alone isn't the right ambiguity test: hover-reveal UI
        # commonly duplicates one interactive element as a hidden/visible
        # pair sharing identical attributes (e.g. a product-listing "Add to
        # cart" button). That is not genuine ambiguity -- only one of them is
        # ever actually on screen. Only block when 2+ matches are
        # simultaneously visible, which is the real "which one?" case the
        # spec's "refuse to guess" rule exists for.
        visible_count = locator.visible_count()
        if visible_count == 0:
            return VerificationResult(False, "selector matched 0 visible elements, expected at least 1")
        if visible_count > 1:
            # Still not necessarily dangerous: a site can legitimately repeat
            # the *same* item in two places at once (e.g. a product's "Add to
            # cart" button shown both in the main grid and in a "recommended
            # items" carousel further down the same page). That is two
            # identical copies of one logical element, not two different
            # candidates to choose between -- comparing fingerprints tells
            # the difference generically, for any site, without knowing its
            # specific layout.
            visible_fingerprints = [build_fingerprint(loc) for loc in locator.visible_locators()]
            first_fp = visible_fingerprints[0]
            all_identical_copies = all(
                fp.tag == first_fp.tag
                and fp.role == first_fp.role
                and fp.accessible_name == first_fp.accessible_name
                and fp.attributes == first_fp.attributes
                for fp in visible_fingerprints
            )
            if not all_identical_copies:
                return VerificationResult(False, "selector matched multiple different visible elements; refusing to guess")
            used_first_match = True

    if action in ("click", "hover", "type", "select", "check"):
        if not locator.is_visible():
            # A freshly AJAX-rendered listing (e.g. a product grid) can still
            # be settling its layout the instant after this exact element was
            # indexed -- a brief, bounded wait here (not the long, open-ended
            # kind the spec forbids) catches that without masking a genuinely
            # absent/hidden element, which still times out below.
            try:
                locator.wait_for(state="visible", timeout=1500)
            except Exception:
                twin = _find_visible_twin(page, locator, selector)
                if twin is None:
                    return VerificationResult(False, "element is not visible")
                locator, selector = twin
        if not locator.is_enabled():
            return VerificationResult(False, "element is not enabled")

    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    if action in _ACTION_COMPATIBLE_TAGS and tag not in _ACTION_COMPATIBLE_TAGS[action]:
        return VerificationResult(
            False, f"element <{tag}> is not compatible with action '{action}'"
        )

    fingerprint = build_fingerprint(locator)

    if not _plausible_match(fingerprint, target_description):
        # Naming the rejected element's own concrete attributes (not just
        # role/name, which are often both None for a plain input) gives the
        # retry feedback something specific to argue against -- "you picked
        # type=email/placeholder='...'" is far more corrective than restating
        # the same target description the model already saw and misjudged.
        descriptor_bits = [f"tag=<{fingerprint.tag}>"]
        if fingerprint.attributes.get("type"):
            descriptor_bits.append(f"type={fingerprint.attributes['type']!r}")
        if fingerprint.attributes.get("placeholder"):
            descriptor_bits.append(f"placeholder={fingerprint.attributes['placeholder']!r}")
        if fingerprint.attributes.get("name"):
            descriptor_bits.append(f"name={fingerprint.attributes['name']!r}")
        return VerificationResult(
            False,
            f"element ({', '.join(descriptor_bits)}, role={fingerprint.role}, "
            f"name={fingerprint.accessible_name!r}) is not plausibly consistent with target "
            f"description {target_description!r} -- pick a DIFFERENT element whose own type/name/"
            f"placeholder actually relates to {target_description!r}",
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
        return VerificationResult(
            True, "verified", fingerprint=fingerprint, similarity=similarity, used_first_match=used_first_match,
            resolved_selector=selector if selector != original_selector else None,
        )

    return VerificationResult(
        True, "verified", fingerprint=fingerprint, used_first_match=used_first_match,
        resolved_selector=selector if selector != original_selector else None,
    )


_SYNONYMS = FIELD_CONCEPT_SYNONYMS


def _plausible_match(fingerprint: Fingerprint, target_description: str) -> bool:
    """Basic lexical/role guard against confident-but-wrong answers: at least one
    significant word from the target description (or a known synonym of it)
    must appear in the element's accessible name, visible text, or stable
    attributes."""
    non_discriminating = {"field", "input", "box", "control", "element"}
    # Punctuation-stripped tokens (`target_description.split()` alone would keep
    # a trailing ":" glued to a word, e.g. "Title:", so it could never match a
    # clean attribute value); length >= 2 so two-letter but significant tokens
    # like a radio value "Mr"/"Ms"/"Dr" aren't silently discarded.
    words = {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9]+", target_description)
        # Single-character tokens are dropped as noise, except "x" -- a
        # common, recognized shorthand for a close/delete icon in test
        # prose ("click the 'X' icon"), not a generic short word.
        if (len(w) >= 2 or w.lower() == "x") and w.lower() not in non_discriminating
    }
    if not words:
        return True
    expanded_words = set(words)
    for word in words:
        expanded_words |= _SYNONYMS.get(word, set())
    haystack = " ".join(
        filter(
            None,
            [
                fingerprint.accessible_name,
                fingerprint.visible_text,
                fingerprint.attributes.get("placeholder"),
                fingerprint.attributes.get("name"),
                fingerprint.attributes.get("id"),
                fingerprint.attributes.get("icon_hint"),
                fingerprint.attributes.get("context"),
            ],
        )
    ).lower()
    return any(word in haystack for word in expanded_words)
