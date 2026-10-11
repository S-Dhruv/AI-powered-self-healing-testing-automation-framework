"""Tier 3: AI identification. Injects a DOM walker script that creates a numbered
list of interactive elements, sends it to the LLM, and receives an index + confidence.
Verification happens in resolve.py (Tier 4) — this module only proposes, never
accepts on its own."""

from __future__ import annotations

import time
from dataclasses import dataclass

from framework.browser import SeleniumPage as Page
from framework.llm.client import LlmClient, LlmProposal, estimate_cost
from framework.llm.usage_logger import UsageLogEntry, UsageLogger
from framework.security.masking import mask
from framework.store.models import Strategy

# Playwright token budget: a configurable cap on characters sent. Confirmed
# live on bbc.com's menu-open homepage (after capping the previously-
# unbounded "nearby heading" field, see _inject_element_walker/getNearestHeading):
# a dense, real-content page can still land within ~1,400 chars of the old
# 12,000 cap, and a live news homepage's own content changes continuously
# between runs -- unlike a static demo site, there's no fixed content size
# to budget exactly against. Raised for margin, not because the per-element
# format itself got any larger.
# Keep the request close to the compact representation described in the
# project specification. The previous 16K-character ceiling produced roughly
# 5K input tokens on the real site and made a quota failure much more likely.
DEFAULT_CHAR_BUDGET = 6_000


@dataclass
class AiFinderResult:
    proposal: LlmProposal
    usage_entry: UsageLogEntry
    estimated_cost: float | None


def _wait_for_dom_stability(page: Page, timeout_ms: int = 5000) -> None:
    """Bounded wait for network/DOM stability before reading the page."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    # Neither wait above fires for a pure CSS-transition reveal with no
    # navigation and no network activity (confirmed live: a slide-out menu
    # opened by a click stayed mid-transition, reporting display:none on its
    # wrapper, for the ~300ms right after the click returned) -- a short,
    # fixed, bounded pause here, not an open-ended one, covers the common
    # animated-reveal-UI duration on any site using one, without materially
    # slowing down a run where nothing was animating at all.
    time.sleep(0.3)


def _inject_element_walker(page: Page) -> None:
    """Injects a script that walks the DOM and tags visible/clickable/typeable
    elements with data-sh-idx attribute. Returns the numbered list as a string."""
    script = """
    (() => {
        const originalScrollX = window.scrollX, originalScrollY = window.scrollY;
        const isVisible = (el) => {
            if (!el.offsetParent && el.style.display !== 'block') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            let rect = el.getBoundingClientRect();
            if (!(rect.width > 0 && rect.height > 0)) return false;
            // Confirmed live on automationexercise.com's product grid: a
            // hover-reveal duplicate control (e.g. a second "Add to cart"
            // link for the same product, stacked behind the one actually on
            // screen) can pass every CSS check above -- display/visibility/
            // opacity/size all look identical to its visible twin -- while
            // still not being the element a real click would land on. A
            // hit-test at the element's own center is what actually
            // determines that, matching what Selenium's own is_displayed()
            // already does.
            let cx = rect.left + rect.width / 2;
            let cy = rect.top + rect.height / 2;
            let inViewport = cx >= 0 && cy >= 0 && cx < window.innerWidth && cy < window.innerHeight;
            if (!inViewport) {
                // Most of a long listing page's products are below the fold
                // at indexing time -- skipping the hit-test for everything
                // off-screen would leave exactly the duplicates this check
                // exists for undetected for every product except whichever
                // ones happen to already be on-screen. Scrolling the
                // candidate into view (instant, no animation) lets the same
                // hit-test run for it too.
                el.scrollIntoView({behavior: 'instant', block: 'center'});
                rect = el.getBoundingClientRect();
                cx = rect.left + rect.width / 2;
                cy = rect.top + rect.height / 2;
                inViewport = cx >= 0 && cy >= 0 && cx < window.innerWidth && cy < window.innerHeight;
            }
            if (inViewport) {
                // Confirmed live on automationexercise.com's signup form: a
                // short viewport (window.innerHeight as low as ~637px) plus
                // scrollIntoView's own centering math can place cx/cy exactly
                // ON the viewport boundary (e.g. cy === innerHeight to the
                // pixel) -- elementFromPoint returns null for a point that's
                // technically one pixel outside the renderable area, even
                // though inViewport's own <= check called it in-bounds. That
                // null then read as "occluded," wrongly excluding multiple
                // perfectly visible, identically-positioned controls (three
                // date-of-birth dropdowns sharing one row, all at once).
                // Clamping to the last valid in-bounds pixel removes the
                // boundary entirely rather than chasing the next off-by-one.
                const clampedX = Math.min(Math.max(cx, 0), window.innerWidth - 1);
                const clampedY = Math.min(Math.max(cy, 0), window.innerHeight - 1);
                const topEl = document.elementFromPoint(clampedX, clampedY);
                // Accept topEl === el or a descendant of el (e.g. the <i>
                // icon inside an <a>, still part of the same clickable
                // control). Do NOT accept the reverse (el inside topEl) --
                // that means the hit-test landed on a generic ANCESTOR
                // container, not on this element or anything of its own, so
                // nothing of this element actually painted at that point.
                // Confirmed live: this is exactly how automationexercise's
                // hover-reveal duplicate "Add to cart" link behaves when
                // off-screen -- the point resolves to its own grid
                // container div, not the link, even though every CSS check
                // above says it's "visible".
                if (!topEl || !(el === topEl || el.contains(topEl))) return false;
            }
            return true;
        };

        const isInteractive = (el) => {
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute('role');
            const clickable = ['a', 'button', 'input', 'select', 'textarea'].includes(tag);
            const roleClickable = ['button', 'link', 'checkbox', 'textbox', 'option'].includes(role);
            return clickable || roleClickable || el.hasAttribute('data-testid');
        };

        document.querySelectorAll('[data-sh-idx]').forEach(el => el.removeAttribute('data-sh-idx'));
        let idx = 0;
        // isInteractive first: it's a cheap attribute check, so only the
        // (possibly scroll-triggering) isVisible call pays for itself on
        // elements that could actually end up indexed.
        document.querySelectorAll('*').forEach(el => {
            if (isInteractive(el) && isVisible(el)) {
                el.setAttribute('data-sh-idx', idx.toString());
                idx++;
            }
        });
        window.scrollTo(originalScrollX, originalScrollY);

        return idx;
    })();
    """
    page.evaluate(script)


def build_compact_page_representation(
    page: Page, char_budget: int = DEFAULT_CHAR_BUDGET
) -> tuple[str, str]:
    """Return ("", a masked, indexed interactive-element list).

    The first element of the pair is deliberately empty, not a second,
    separately-labelled copy of the same content. Selenium has no native
    ARIA-snapshot API, so an earlier version of this function called
    build_element_list() a second time and relabelled the IDENTICAL result
    as an "ARIA snapshot" -- confirmed live (bbc.com): this silently
    doubled every prompt's size for zero new information, which halves the
    real headroom before build_user_prompt's char_budget truncates a dense
    page's element list (a real, live news homepage's own content changes
    continuously, unlike a static demo site, so there's no fixed size to
    budget exactly against in the first place). build_user_prompt skips
    the ARIA block entirely when this is empty, the same way it already
    skips the interactive-elements block when that one is empty.
    """
    _wait_for_dom_stability(page)
    _inject_element_walker(page)
    interactive = build_element_list(page, char_budget)
    return "", interactive


def build_element_list(page: Page, char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """Walks the DOM and returns a numbered list of interactive elements."""
    _wait_for_dom_stability(page)
    _inject_element_walker(page)

    script = """
    (() => {
        const getNearestHeading = (el) => {
            let node = el;
            for (let i = 0; i < 6 && node; i++) {
                node = node.parentElement;
                if (!node) break;
                const h = node.querySelector('h1, h2, h3, legend');
                // Confirmed live on bbc.com: an uncapped heading, repeated
                // verbatim as this same string on every single element that
                // shares it (every nav-menu item, dozens of them, all 6
                // levels under one page-wide h1), can burn a large fraction
                // of the whole character budget on one repeated, unhelpful
                // value -- truncating the element list before it ever
                // reaches a genuinely useful element further down (a search
                // field, a later nav link). Capped the same way `text` and
                // `getNearbyText` already are elsewhere in this file.
                if (h) return h.innerText.trim().slice(0, 50);
            }
            return null;
        };

        // Inputs (radio/checkbox especially) have no innerText of their own --
        // the human-readable "Mr" next to a title radio, or "I agree" next to
        // a checkbox, lives in an associated <label>, not the input itself.
        // Without this, the LLM only ever sees the bare input with no way to
        // tell two radio options apart.
        const getAssociatedLabel = (el) => {
            if (el.id) {
                const forLabel = document.querySelector(`label[for="${el.id.replace(/"/g, '\\\\"')}"]`);
                if (forLabel) return forLabel.innerText.trim().slice(0, 60) || null;
            }
            const wrappingLabel = el.closest('label');
            if (wrappingLabel) {
                const clone = wrappingLabel.cloneNode(true);
                clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
                const text = clone.innerText.trim();
                if (text) return text.slice(0, 60);
            }
            // Fall back to immediately adjacent text (covers markup with no
            // <label> at all, just a bare input followed/preceded by text).
            const sibling = el.nextSibling || el.previousSibling;
            if (sibling && sibling.nodeType === Node.TEXT_NODE) {
                const text = sibling.textContent.trim();
                if (text) return text.slice(0, 60);
            }
            return null;
        };

        // Icon-only buttons/links (no text, no aria-label) are a very common
        // pattern -- their only hint of meaning is an icon-font class name on
        // themselves or a child <i>/<span> (Font Awesome "fa-arrow-...",
        // Bootstrap Glyphicons "glyphicon-...", or a generic "icon-...").
        // That class name is otherwise completely invisible to both the LLM
        // and the plausibility check, generically, on any site using these
        // (extremely common) icon-font conventions, not one site's markup.
        const ICON_CLASS_RE = /\\b(?:fa|glyphicon|icon)-([a-z0-9][a-z0-9-]*)\\b/i;
        const getIconHint = (el) => {
            const ownMatch = (el.className || '').match(ICON_CLASS_RE);
            if (ownMatch) return ownMatch[1].replace(/-/g, ' ');
            const iconChild = el.querySelector('i, span');
            if (iconChild) {
                const childMatch = (iconChild.className || '').match(ICON_CLASS_RE);
                if (childMatch) return childMatch[1].replace(/-/g, ' ');
            }
            return null;
        };

        // A no-own-text control repeated once per item in a listing (e.g.
        // an "Add to cart" button on every card in a product grid) is only
        // distinguishable by what else sits in its immediate card -- the
        // product's own name and price, generically, not any one site's
        // class names. The immediate parent is deliberately the only level
        // checked: going further up re-enters the next item's duplicate
        // content on grids that render a hidden hover-reveal copy of each
        // card (the same duplication noted elsewhere in this file).
        const getNearbyText = (el) => {
            const parent = el.parentElement;
            if (!parent) return null;
            const ownText = (el.innerText || el.value || '').trim();
            let residual = (parent.innerText || '').trim();
            if (ownText) residual = residual.replace(ownText, '').trim();
            residual = residual.replace(/\\s+/g, ' ');
            return residual && residual.length <= 120 ? residual.slice(0, 80) : null;
        };

        const lines = [];
        const quote = (value) => "'" + value.replaceAll("'", "\\'") + "'";
        document.querySelectorAll('[data-sh-idx]').forEach(el => {
            const idx = el.getAttribute('data-sh-idx');
            const tag = el.tagName.toLowerCase();
            const testid = el.getAttribute('data-testid');
            const role = el.getAttribute('role');
            const type = el.getAttribute('type');
            const name = el.getAttribute('name');
            const id = el.getAttribute('id');
            const placeholder = el.getAttribute('placeholder');
            const aria_label = el.getAttribute('aria-label');
            const value = el.getAttribute('value');
            const text = (el.innerText || '').trim().slice(0, 60);
            const heading = getNearestHeading(el);
            const label = getAssociatedLabel(el);
            const icon = !text ? getIconHint(el) : null;
            // Gated on label only, not text: a repeated control sharing the
            // SAME generic text across every item in a listing (every card's
            // button literally reading "Add to cart") is exactly the case
            // nearby text disambiguates -- an icon-only control with no text
            // at all benefits too, same as the icon_hint case above.
            const nearby = !label ? getNearbyText(el) : null;

            const attrs = [];
            if (type) attrs.push(`type=${quote(type)}`);
            if (name) attrs.push(`name=${quote(name)}`);
            if (id) attrs.push(`id=${quote(id)}`);
            if (value && (type === 'radio' || type === 'checkbox' || tag === 'option')) {
                attrs.push(`value=${quote(value)}`);
            }
            if (placeholder) attrs.push(`placeholder=${quote(placeholder)}`);
            if (aria_label) attrs.push(`aria-label=${quote(aria_label)}`);
            if (text) attrs.push(`text=${quote(text)}`);
            if (label && label !== text) attrs.push(`label=${quote(label)}`);
            if (icon) attrs.push(`icon=${quote(icon)}`);
            if (nearby) attrs.push(`context=${quote(nearby)}`);
            if (heading) attrs.push(`section=${quote(heading)}`);

            const attrStr = attrs.length > 0 ? ' ' + attrs.join(' ') : '';
            const roleStr = role ? ` role=${quote(role)}` : '';
            const testidStr = testid ? ` testid=${quote(testid)}` : '';

            lines.push(`${idx}. <${tag}>${roleStr}${testidStr}${attrStr}`);
        });

        return lines.join('\\n');
    })();
    """

    element_list = page.evaluate(script) or ""
    element_list = mask(element_list)
    if len(element_list) > char_budget:
        element_list = element_list[:char_budget] + "\n...[truncated to token budget]"

    return element_list


def selector_for_index(page: Page, index: int) -> str | None:
    """Derive a stable selector from a tagged element; never persist its index.

    Tries, in order: own stable attributes -> a scoped-parent CSS selector ->
    an exact visible-text match (the "text" strategy from spec §2 fallback
    order). Many real buttons (e.g. a plain `<button type="submit">Login</button>`)
    carry no id/name/testid, so without the text fallback a correctly-identified
    element would be silently undiscoverable and the resolver would retry onto
    the wrong element entirely.
    """
    script = """
    (index) => {
        const element = document.querySelector(`[data-sh-idx="${index}"]`);
        if (!element) return null;
        // CSS.escape() is for identifiers (#id, .class) -- using it on an
        // attribute VALUE inside [attr="..."] is the wrong tool (it escapes a
        // leading digit as e.g. "\\31 ", which only works by the accident of
        // also being valid inside a quoted CSS string). Attribute values need
        // ordinary string escaping instead.
        const quoteIdent = (value) => CSS.escape(String(value));
        const quoteAttrValue = (value) => String(value).replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
        const tag = element.tagName.toLowerCase();
        const parts = [`${tag}`];
        for (const attr of ['data-testid', 'id', 'name', 'type', 'placeholder', 'aria-label', 'href']) {
            const value = element.getAttribute(attr);
            if (value) parts.push(`[${attr}="${quoteAttrValue(value)}"]`);
        }
        // Many real sites key a repeated item (e.g. one "Add to cart" button
        // per product in a listing) to a custom data-* attribute of their own
        // naming (data-product-id, data-item, data-sku, ...) rather than one
        // of the fixed names above. data-sh-idx is this walker's own injected
        // marker, not page content, and is excluded -- using it would smuggle
        // a bare DOM index back in as a "selector", which the spec forbids.
        for (const attr of element.attributes) {
            if (attr.name.startsWith('data-') && attr.name !== 'data-testid' && attr.name !== 'data-sh-idx' && attr.value) {
                parts.push(`[${attr.name}="${quoteAttrValue(attr.value)}"]`);
            }
        }
        const own = parts.join('');
        // Raw, unfiltered uniqueness first: a selector that's genuinely
        // unique in the DOM is always safe to hand back bare, since nothing
        // about it depends on this walker's own transient [data-sh-idx]
        // marker still being there later when verify_locator re-queries it.
        if (document.querySelectorAll(own).length === 1) return own;
        // .closest() always matches the element itself first if it already
        // satisfies the selector (nearly every element has *some* class) --
        // starting from parentElement is required to actually reach an
        // ancestor instead of re-selecting the same element.
        const parent = element.parentElement && element.parentElement.closest('[id], [data-testid], [class]');
        let scoped = null;
        if (parent) {
            let scope = parent.tagName.toLowerCase();
            if (parent.id) scope += `#${quoteIdent(parent.id)}`;
            else if (parent.getAttribute('data-testid')) {
                scope += `[data-testid="${quoteAttrValue(parent.getAttribute('data-testid'))}"]`;
            } else {
                const cls = [...parent.classList].filter(Boolean).slice(0, 2);
                if (cls.length) scope += '.' + cls.map(quoteIdent).join('.');
            }
            scoped = `${scope} ${own}`;
            // Confirmed live on automationexercise.com: two DIFFERENT real
            // controls (a modal's own "View Cart" link and the page's
            // persistent header cart-icon link) can share the exact same
            // href with no other distinguishing attribute -- scoping to a
            // nearby ancestor with its own id/class, checked against the
            // real, unfiltered DOM (not [data-sh-idx]), is what actually
            // rules the OTHER one out here, rather than accepting an
            // own-attribute selector that's "unique" only among this one
            // indexing pass's reachable elements and then matches both of
            // them again the moment something re-queries it bare.
            if (document.querySelectorAll(scoped).length === 1) return scoped;
        }
        // Neither the bare nor the scoped selector is unique in the raw
        // DOM -- this is the genuine hover-reveal-duplicate case (e.g. a
        // second "Add to cart" link for the same product, stacked behind
        // the one actually on screen): both copies share identical own
        // attributes AND an identical nearby-ancestor scope, so no CSS
        // selector can ever tell them apart structurally. [data-sh-idx]
        // marks exactly the elements this walker's own hit-test already
        // judged individually reachable -- falling back to that judgment
        // here is a last resort, not the first check, and is still safe:
        // verify_locator's own visible-twin / identical-fingerprint
        // handling is exactly what accepts a bare selector matching two
        // structurally-IDENTICAL copies, which is what this path is for.
        if (document.querySelectorAll(`${own}[data-sh-idx]`).length === 1) return own;
        if (scoped && document.querySelectorAll(`${scoped}[data-sh-idx]`).length === 1) return scoped;
        return null;
    }
    """
    css_selector = page.evaluate(script, index)
    if css_selector is not None:
        return css_selector

    text_script = """
    (index) => {
        const element = document.querySelector(`[data-sh-idx="${index}"]`);
        if (!element) return null;
        const tag = element.tagName.toLowerCase();
        const text = (element.innerText || element.value || '').trim();
        return text ? { tag, text } : null;
    }
    """
    info = page.evaluate(text_script, index)
    if not info or not info.get("text"):
        return None
    text_selector = f"{info['tag']}:text('{info['text']}')"
    try:
        if page.locator(text_selector).count() == 1:
            return text_selector
    except Exception:
        return None
    return None


def strategies_for_locator(locator, selector: str) -> list[Strategy]:
    """Build replay strategies from the selected, already-verified element."""
    attrs = locator.evaluate(
        """el => ({
            testid: el.getAttribute('data-testid'),
            id: el.getAttribute('id'),
            name: el.getAttribute('name'),
            type: el.getAttribute('type'),
            placeholder: el.getAttribute('placeholder')
        })"""
    )
    # A bare name+type (or placeholder) selector is frequently NOT unique on
    # its own -- e.g. a login form and a signup form on the same page both
    # using name="email" -- so prefer a candidate that also folds in the
    # element's own custom data-* attributes (data-qa, data-cy, ...), the
    # same generic signal selector_for_index() already uses, before falling
    # back to the bare, weaker version.
    extra_data_attrs: dict[str, str] = locator.evaluate(
        "el => Object.fromEntries([...el.attributes]"
        ".filter(a => a.name.startsWith('data-') && a.name !== 'data-testid' && a.name !== 'data-sh-idx')"
        ".map(a => [a.name, a.value]))"
    ) or {}
    data_attr_suffix = "".join(
        f'[{name}="{value}"]' for name, value in sorted(extra_data_attrs.items()) if value
    )
    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    visible_text = (locator.evaluate("el => (el.innerText || el.value || '').trim()") or "").strip()
    candidates: list[Strategy] = []
    if attrs.get("testid"):
        candidates.append(Strategy(kind="test_id", value=attrs["testid"]))
    if attrs.get("id"):
        candidates.append(Strategy(kind="css", value=f"#{attrs['id']}"))
    if attrs.get("name") and attrs.get("type"):
        base = f"{tag}[name='{attrs['name']}'][type='{attrs['type']}']"
        if data_attr_suffix:
            candidates.append(Strategy(kind="css", value=base + data_attr_suffix))
        candidates.append(Strategy(kind="css", value=base))
    if attrs.get("placeholder"):
        candidates.append(
            Strategy(kind="css", value=f"{tag}[placeholder='{attrs['placeholder']}']")
        )
    if visible_text:
        candidates.append(Strategy(kind="text", value=f"{tag}:text('{visible_text}')"))
    candidates.append(Strategy(kind="css", value=selector))
    unique: list[Strategy] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.kind, candidate.value)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def try_ai_finder(
    client: LlmClient,
    page: Page,
    *,
    target_description: str,
    action: str,
    run_id: str,
    test_case_id: str,
    step_no: int,
    user_id: str,
    reason: str,  # "first_learning" | "healing"
    char_budget: int = DEFAULT_CHAR_BUDGET,
    retry_feedback: str | None = None,
) -> AiFinderResult:
    aria_snapshot, element_list = build_compact_page_representation(page, char_budget)

    proposal = client.find_element(
        target_description=target_description,
        action=action,
        aria_snapshot=aria_snapshot,
        element_list=element_list,
        retry_feedback=retry_feedback,
    )
    if proposal.index is not None:
        try:
            proposal.selector = selector_for_index(page, proposal.index)
        except Exception as exc:
            proposal.selector = None
            proposal.status = "error"
            proposal.reason = f"failed to derive selector for AI index {proposal.index}: {exc}"

    cost = (
        estimate_cost(client.model, proposal.input_tokens, proposal.output_tokens)
        if proposal.status == "success"
        else 0.0
    )

    entry = UsageLogEntry(
        timestamp=UsageLogger.now_iso(),
        run_id=run_id,
        test_case_id=test_case_id,
        step_no=step_no,
        user_id=user_id,
        agent_name="ai_finder_retry" if retry_feedback else "ai_finder",
        reason=reason,  # type: ignore[arg-type]
        model=client.model,
        input_tokens=proposal.input_tokens,
        output_tokens=proposal.output_tokens,
        latency_ms=proposal.latency_ms,
        status=proposal.status,  # type: ignore[arg-type]
        estimated_cost=cost if cost is not None else 0.0,
    )

    return AiFinderResult(proposal=proposal, usage_entry=entry, estimated_cost=cost)
