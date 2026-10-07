from framework.resolver.ai_finder import build_compact_page_representation


def test_interactive_list_includes_identifying_attributes_not_blank_text(login_page):
    """Regression: <input> elements have no innerText, so the previous version
    of this list printed `text=''` for every input -- useless for the LLM to
    pick between them. Real identifying attributes must be present."""
    _, interactive_list = build_compact_page_representation(login_page)
    assert "placeholder='Email Address'" in interactive_list
    assert "placeholder='Password'" in interactive_list
    assert "text=''" not in interactive_list


def test_interactive_list_disambiguates_identical_twin_fields_by_section(twin_forms_page):
    """Regression: a real run against automationexercise.com showed two
    <input type='email' name='email' placeholder='Email Address'> elements --
    one under Login, one under Signup -- with IDENTICAL attributes and no way
    to tell them apart, causing every AI proposal to match both and get
    rejected ('selector matched 2 elements'). The nearest-heading 'section'
    context must distinguish them."""
    _, interactive_list = build_compact_page_representation(twin_forms_page)
    lines = [ln for ln in interactive_list.splitlines() if "type='email'" in ln and "name='email'" in ln]
    assert len(lines) == 2
    sections = {ln.split("section=")[-1] for ln in lines}
    assert sections == {"'Login to your account'", "'New User Signup!'"}
