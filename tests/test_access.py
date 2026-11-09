import pytest

from gformbot.access import FormAccessError, classify_access, raise_for_access


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
