"""Utilities to dismiss common ad overlays and popups that interfere with element detection."""

from __future__ import annotations

from playwright.sync_api import Page


def dismiss_google_vignette(page: Page, timeout_ms: int = 5000) -> bool:
    """Dismiss a Google Vignette ad overlay if present. Returns True if an
    overlay was detected and handled.

    Google Vignette marks itself by appending '#google_vignette' to the
    current URL when it opens. Its close ("X") button lives inside a
    cross-origin ad iframe, which Playwright generally cannot click into
    directly (same-origin policy) -- a selector-based click attempt silently
    does nothing there. The reliable signal and fix is the URL fragment
    itself: stripping it (via history.replaceState, so the page is not
    reloaded) closes the vignette the same way the ad network's own script
    detects the user navigating past it.
    """
    try:
        if "#google_vignette" in page.url:
            page.evaluate(
                "() => { if (location.hash.includes('google_vignette')) "
                "{ history.replaceState(null, '', location.pathname + location.search); } }"
            )
            page.wait_for_timeout(200)  # let the ad script's own teardown observer fire
            return True
    except Exception:
        pass

    # Fallback best-effort for other overlay types: a same-origin close button, else ESC.
    try:
        close_btn = page.locator('[aria-label*="Close"], [data-close-button]')
        if close_btn.count() > 0 and close_btn.first.is_visible():
            close_btn.first.click(timeout=timeout_ms)
            return True
    except Exception:
        pass

    return False


def clear_overlays_before_interaction(page: Page, timeout_ms: int = 2000) -> None:
    """Attempt to dismiss any known ad overlays or popups before attempting element interaction.
    This is a best-effort utility; it does not fail if no overlays are found."""
    dismiss_google_vignette(page, timeout_ms)
