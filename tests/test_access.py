import os
from types import SimpleNamespace

import pytest

from gformbot.access import FormAccessError, check_browser_access, classify_access, raise_for_access


@pytest.mark.parametrize(
    ("document", "url", "status", "state"),
    [
        ("", "https://accounts.google.com/v3/signin/identifier", None, "login_required"),
        ("<h1>Sign in to continue</h1>", "", None, "login_required"),
        ("<h1>You need permission</h1>", "", None, "permission_denied"),
        ("<h1>You've already responded</h1>", "", None, "already_answered"),
        ("<p>This form is no longer accepting responses.</p>", "", None, "closed"),
        ("<p>This form is not published.</p>", "", None, "closed"),
        ("<h1>Something went wrong</h1>", "", None, "temporary_failure"),
        ("", "https://docs.google.com/forms/d/e/id/closedform", None, "closed"),
        ("", "", 401, "login_required"),
        ("", "", 403, "permission_denied"),
        ("", "", 404, "closed"),
        ("", "", 410, "closed"),
        ("", "", 429, "temporary_failure"),
        ("", "", 503, "temporary_failure"),
    ],
)
def test_proven_access_states(document, url, status, state):
    assert classify_access(document, url, status) == state
    with pytest.raises(FormAccessError) as failure:
        raise_for_access(document, url, status)
    assert failure.value.state == state


@pytest.mark.parametrize(
    "document",
    [
        "<h1>An unfamiliar page</h1>",
        '<div role="listitem"><h2>You need permission</h2><input name="entry.1"></div>',
        "<form><h2>Why does it say sign in to continue?</h2><textarea></textarea></form>",
        '<script>var x = "no longer accepting responses";</script>',
        '<style>.login:after {content:"sign in to continue"}</style>',
        "<div hidden>You need permission</div>",
        '<div aria-hidden="true">You need permission</div>',
        '<div style="display:none">You need permission</div>',
        "<script>FB_PUBLIC_LOAD_DATA_ = [];</script><form><p>Example: you need permission.</p></form>",
        '<input type="hidden" value="Sign in to continue">',
        '<form><div role="listitem">Example: no longer accepting responses.</div></form>',
        '<div class="vHW8K">You have already responded. Join the group next.</div>',
        '<div class="freebirdFormviewerViewResponseConfirmationMessage">This form is closed.</div>',
    ],
)
def test_question_text_hidden_content_and_unknown_pages_are_not_proof(document):
    assert classify_access(document) is None
    raise_for_access(document)


def test_dedicated_status_message_is_checked_even_with_embedded_metadata():
    document = (
        "<script>FB_PUBLIC_LOAD_DATA_ = [];</script>"
        '<div class="freebirdFormviewerViewResponseMessage">You have already responded.</div>'
    )
    assert classify_access(document) == "already_answered"


def test_minified_status_page_with_metadata_does_not_look_like_a_live_form():
    document = (
        '<script>FB_PUBLIC_LOAD_DATA_ = [];</script><div class="DqBBlb">This form is not yet published.</div>'
    )
    assert classify_access(document) == "closed"


def test_hidden_question_control_does_not_hide_a_real_access_error():
    document = '<div hidden><input name="entry.1"></div><h1>You need permission</h1>'
    assert classify_access(document) == "permission_denied"


def test_unknown_error_is_actionable_and_never_echoes_page_or_identity():
    with pytest.raises(FormAccessError) as failure:
        raise_for_access("<h1>secret@example.test</h1>", unknown=True)
    assert failure.value.state == "unknown"
    assert "secret" not in str(failure.value)
    assert "No public form schema" in str(failure.value)


def test_browser_check_uses_the_current_document():
    driver = SimpleNamespace(
        current_url="https://docs.google.com/forms/d/e/id/viewform",
        page_source="<h1>You need permission</h1>",
    )
    with pytest.raises(FormAccessError, match="restricted"):
        check_browser_access(driver)


def test_browser_redirect_does_not_read_authentication_page():
    class Driver:
        current_url = "https://accounts.google.com/ServiceLogin"

        @property
        def page_source(self):
            raise AssertionError("An authentication page is not needed for classification")

    with pytest.raises(FormAccessError) as failure:
        check_browser_access(Driver())
    assert failure.value.state == "login_required"


@pytest.mark.parametrize("text,state", [("You need permission", "permission_denied"), ("", None)])
def test_browser_fast_snapshot_never_transfers_embedded_metadata(text, state):
    class Driver:
        current_url = "https://docs.google.com/forms/d/e/id/viewform"

        def execute_script(self, script):
            assert "FB_PUBLIC_LOAD_DATA_" not in script
            return text

        @property
        def page_source(self):
            pytest.fail("The browser snapshot must not fetch the entire document")

    if state:
        with pytest.raises(FormAccessError) as failure:
            check_browser_access(Driver())
        assert failure.value.state == state
    else:
        check_browser_access(Driver())


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Opt-in local Chrome check")
def test_browser_access_snapshot_handles_visibility_questions_and_confirmations(html_page):
    from gformbot.browser import get_driver

    driver = get_driver("chrome")
    try:
        cases = [
            ("<h1>You need permission</h1>", "permission_denied"),
            ('<div class="DqBBlb">No longer accepting responses</div>', "closed"),
            ('<div role="listitem">You need permission</div><input name="entry.1">', None),
            ('<form><div role="listitem">You need permission</div></form>', None),
            ('<div class="vHW8K">You have already responded. Join our group.</div>', None),
            (
                '<div class="freebirdFormviewerViewResponseConfirmationMessage">This form is closed.</div>',
                None,
            ),
            ('<style>.hidden {display:none}</style><div class="hidden">You need permission</div>', None),
            ("<input hidden><h1>Something went wrong</h1>", "temporary_failure"),
        ]
        for document, state in cases:
            driver.get(html_page(document))
            if state:
                with pytest.raises(FormAccessError) as failure:
                    check_browser_access(driver)
                assert failure.value.state == state
            else:
                check_browser_access(driver)
    finally:
        driver.quit()
