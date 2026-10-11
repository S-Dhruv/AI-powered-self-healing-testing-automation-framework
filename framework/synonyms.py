"""Generic web-form concept synonyms, shared by the verifier's plausibility
check and the executor's Test Data label matching.

These are concepts that recur across arbitrary sites/forms (a search box is
universally described as "keyword" in test prose, never labelled that in
markup or Test Data; a newsletter input is the target of a "subscription"
ask) -- not selectors or hints for one named application. Keeping them here
does not violate "no app-specific code in framework/": apps/<name>/ is still
the only place a URL, selector hint, or account lives.
"""

from __future__ import annotations

FIELD_CONCEPT_SYNONYMS: dict[str, set[str]] = {
    "keyword": {"search", "query"},
    # "susbscribe" covers a real typo found on a live site's own markup
    # (id="susbscribe_email") -- a misspelled attribute/label is still the
    # correct field; this is defensive the same way the others are, not a
    # hint about which site.
    "subscription": {"subscribe", "newsletter", "susbscribe"},
    "quantity": {"qty", "quantity"},
    "review": {"comment", "feedback", "review"},
    # An icon-only "close"/"remove"/"delete" control is near-universally
    # rendered via an icon font, whose class name (captured as `icon_hint`/
    # `context`, see ai_finder.py and fingerprint.py) names the glyph itself
    # ("times", "remove", "trash", "close", "x") rather than its purpose --
    # a generic UI convention, not one site's icon choice.
    "x": {"times", "close", "remove", "cross", "delete"},
    "delete": {"remove", "times", "close", "trash", "x"},
}
