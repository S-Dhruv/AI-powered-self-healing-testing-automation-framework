"""Utilities to dismiss common ad overlays and popups that interfere with element detection."""

from __future__ import annotations

from framework.browser import SeleniumPage as Page


_AD_IFRAME_SELECTOR = (
    'iframe[title="Advertisement"], '
    'iframe[id^="aswift_"], '
    'iframe[src*="googleads.g.doubleclick.net"], '
    'iframe[src*="googlesyndication.com"]'
)
_CLOSE_SELECTOR = (
    '[aria-label="Close"], [aria-label*="close" i], '
    '[title="Close"], [title*="close" i], '
    '[data-close-button], [data-dismiss], .close, .close-button'
)


def dismiss_google_vignette(page: Page, timeout_ms: int = 5000) -> bool:
    """Dismiss a Google Vignette ad overlay if present. Returns True if an
    overlay was detected and handled.

    Google Vignette marks itself by appending '#google_vignette' to the
    current URL when it opens, and its own script normally closes it when it
    detects the user navigating away from that hash (e.g. pressing Back,
    which fires a real 'popstate'/'hashchange' event). Its close ("X") button
    lives inside a cross-origin ad iframe that cannot be clicked into
    directly (same-origin policy) -- a selector-based click attempt silently
    does nothing there.

    `history.replaceState()` rewrites the URL WITHOUT firing a
    'popstate'/'hashchange' event (this is standard, documented browser
    behaviour, not a bug) -- so doing only that can leave the ad's own close
    listener never actually notified, even though the URL looks clean
    afterward. Dispatching the events synthetically gives any such listener
    the same signal a real back-navigation would have. As a backstop that
    does not depend on the ad's script cooperating at all, also look for a
    genuine full-viewport, high-z-index overlay element and remove it
    directly -- gated specifically to when the vignette hash was actually
    seen, so a legitimate same-site modal is never mistaken for one.
    """
    handled = False
    try:
        if "#google_vignette" in page.url:
            page.evaluate(
                """() => {
                    if (!location.hash.includes('google_vignette')) return;
                    history.replaceState(null, '', location.pathname + location.search);
                    window.dispatchEvent(new PopStateEvent('popstate'));
                    window.dispatchEvent(new HashChangeEvent('hashchange'));
                }"""
            )
            handled = True
            # dispatchEvent() runs any synchronous listener before returning
            # (JS has one thread; this call already returned by the time we
            # get here), so any ad script reacting to popstate/hashchange has
            # already had its chance -- no artificial wait needed before the
            # DOM-level fallback below.
            page.evaluate(
                """() => {
                    const vw = window.innerWidth, vh = window.innerHeight;
                    for (const el of document.querySelectorAll('body > *')) {
                        const style = getComputedStyle(el);
                        if (style.position !== 'fixed' && style.position !== 'absolute') continue;
                        const z = parseInt(style.zIndex, 10) || 0;
                        if (z < 1000) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width >= vw * 0.9 && rect.height >= vh * 0.9) {
                            el.remove();
                        }
                    }
                }"""
            )
    except Exception:
        handled = False

    # Dismiss same-origin close controls. Do not use `.first`: ad scripts often
    # leave hidden copies of their close button in the DOM.
    try:
        close_buttons = page.locator(_CLOSE_SELECTOR)
        for index in range(close_buttons.count()):
            button = close_buttons.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=timeout_ms)
                handled = True
    except Exception:
        pass

    # A visible Google ad iframe is cross-origin, so its close control cannot be
    # queried from the parent frame. Removing only the visible ad frame's
    # generated container prevents it from intercepting test clicks while
    # leaving the application's DOM untouched.
    try:
        ad_frames = page.locator(_AD_IFRAME_SELECTOR)
        for index in range(ad_frames.count()):
            frame = ad_frames.nth(index)
            if not frame.is_visible():
                continue
            frame.evaluate(
                """el => {
                    const rect = el.getBoundingClientRect();
                    const parent = el.parentElement;
                    if (!parent || rect.width === 0 || rect.height === 0) return;
                    const style = getComputedStyle(parent);
                    if (style.position === 'fixed' || style.position === 'absolute' ||
                        parent.id.startsWith('aswift_') ||
                        parent.getAttribute('aria-label') === 'Advertisement') {
                        parent.remove();
                    }
                }"""
            )
            handled = True
    except Exception:
        pass

    # Vignettes and some ad SDKs only respond to Escape after gaining focus.
    try:
        page.keyboard_press("Escape")
    except Exception:
        pass

    return handled


def clear_overlays_before_interaction(page: Page, timeout_ms: int = 2000) -> None:
    """Attempt to dismiss any known ad overlays or popups before attempting element interaction.
    This is a best-effort utility; it does not fail if no overlays are found."""
    dismiss_google_vignette(page, timeout_ms)
