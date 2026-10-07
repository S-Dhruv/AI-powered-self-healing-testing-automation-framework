from framework.actions.ad_utils import dismiss_google_vignette


def test_strips_google_vignette_fragment_without_reloading(login_page):
    login_page.evaluate("() => { history.replaceState(null, '', location.pathname + '#google_vignette'); }")
    assert "#google_vignette" in login_page.url

    dismissed = dismiss_google_vignette(login_page)

    assert dismissed is True
    assert "#google_vignette" not in login_page.url


def test_no_vignette_present_returns_false(login_page):
    assert "#google_vignette" not in login_page.url
    dismissed = dismiss_google_vignette(login_page)
    assert dismissed is False
