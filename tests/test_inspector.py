import json

import pytest

from gformbot.access import FormAccessError
from gformbot.config import config_from_schema
from gformbot.inspector import clean_url, parse_form_html, schema_fingerprint

GFormBotError = ValueError

URL = "https://docs.google.com/forms/d/e/test_form/viewform"


def document(items, title="A form"):
    meta = ["Description", items, ["Thanks for testing!"]] + [None] * 5 + [title]
    return "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps([None, meta]) + ";</script>"


def item(identifier, title, kind, options=None, required=True, rules=None):
    return [
        identifier + 100,
        title,
        None,
        kind,
        [
            [
                identifier,
                [[s, None, None, None, 0] for s in options] if options else None,
                int(required),
                None,
                rules,
            ]
        ],
    ]


def test_inspection_reads_every_required_page_without_navigating():
    form = parse_form_html(
        document(
            [
                item(1, "First required", 2, ["A", "B"]),
                [1000, "Page two", None, 8],
                item(
                    2,
                    "Choose up to 2. If choosing the last option, choose it alone.",
                    4,
                    ["Roads, footpaths or crossings", "Trees", "None"],
                    rules=[[7, 201, ["2"]]],
                ),
                [1001, "Page three", None, 8],
                item(3, "Optional comment", 1, required=False),
            ]
        ),
        URL,
    )
    assert len(form.questions) == 3
    assert form.sections == ["Section 1", "Page two", "Page three"]
    assert [(q.page - 1) for q in form.questions] == [0, 1, 2]
    assert form.questions[1].options[0] == "Roads, footpaths or crossings"
    assert form.questions[1].exclusive_options == ["None"]
    assert form.questions[1].max_select == 2
    assert form.confirmation_text == "Thanks for testing!"
    form.require_supported()


def test_last_option_is_not_exclusive_without_instruction():
    form = parse_form_html(
        document([item(1, "What provided shade?", 4, ["Trees", "Other shade structures"])]), URL
    )
    assert form.questions[0].exclusive_options == []


def test_literal_code_and_entities_in_question_and_options_stay_exact():
    title = "What does <b> do?"
    labels = ["Use <b> tags", "Literal &amp; text", "Roads, footpaths or crossings"]
    form = parse_form_html(document([item(1, title, 2, labels)]), URL)
    assert form.questions[0].text == title
    assert form.questions[0].options == labels


def test_two_explicit_exclusive_options_and_minimum_validation():
    form = parse_form_html(
        document(
            [
                item(
                    1,
                    "Select all. If choosing either of the last two options, choose it alone.",
                    4,
                    ["Trees", "Shelters", "None", "Not sure"],
                    rules=[[7, 200, ["1"]]],
                )
            ]
        ),
        URL,
    )
    assert form.questions[0].exclusive_options == ["None", "Not sure"]
    assert form.questions[0].min_select == 1


@pytest.mark.parametrize("args", [[], ["oops"], ["-1"], [True], [1.5], None])
def test_malformed_checkbox_constraint_is_not_silently_ignored(args):
    with pytest.raises(ValueError, match="Invalid checkbox selection constraint"):
        parse_form_html(document([item(1, "Choose", 4, ["A", "B"], rules=[[7, 201, args]])]), URL)


def test_common_field_types_roundtrip():
    items = [
        item(i + 1, f"Question {i}", code, ["1", "2"] if code in (2, 3, 4, 5) else None)
        for i, code in enumerate([0, 1, 2, 3, 4, 5])
    ]
    form = parse_form_html(document(items, "Test &amp; Check"), URL)
    assert [q.qtype for q in form.questions] == ["text", "textarea", "radio", "dropdown", "checkbox", "scale"]
    assert form.title == "Test & Check"
    assert schema_fingerprint(config_from_schema(form)) == schema_fingerprint(form)


def test_unsupported_controls_remain_visible_alongside_routing():
    branch = item(2, "Branch", 2, ["A"])
    branch[4][0][1][0][2] = -3
    form = parse_form_html(document([item(1, "Upload", 13), branch]), URL)
    assert "file upload" in form.unsupported[0]
    assert form.questions[1].branches == {"A": 0}
    with pytest.raises(GFormBotError, match="cannot be filled reliably"):
        form.require_supported()


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/forms/d/e/id/viewform",
        "https://docs.google.com/forms/d/id/edit",
        "http://docs.google.com/forms/d/e/id/viewform",
        "https://docs.google.com.evil.test/forms/d/id/viewform",
        "https://u:p@docs.google.com/forms/d/id/viewform",
    ],
)
def test_bad_urls_are_actionable(value):
    with pytest.raises(GFormBotError):
        clean_url(value)


def test_tracking_and_quotes_do_not_change_form_identity():
    assert clean_url('"' + URL + '?usp=sharing&entry.1=A"') == URL


def test_unavailable_or_changed_schema_is_not_an_empty_success():
    with pytest.raises(GFormBotError, match="No public form schema"):
        parse_form_html("<h1>Form closed</h1>", URL)
    with pytest.raises(GFormBotError, match="unrecognized structure"):
        parse_form_html("FB_PUBLIC_LOAD_DATA_ = [null,[]]", URL)


def test_unsupported_questions_are_still_available_for_display():
    form = parse_form_html(document([item(1, "Upload a document", 13)]), URL)
    assert form.questions[0].text == "Upload a document"
    assert form.questions[0].qtype == "upload"
    assert form.questions[0].unsupported  # Missing upload settings still block execution.
    with pytest.raises(ValueError, match="file upload"):
        form.require_supported()


def test_schema_fingerprint_ignores_sampling_preferences_but_detects_form_change():
    schema = parse_form_html(document([item(1, "Choose", 2, ["A", "B"])]), URL)
    config = config_from_schema(schema)
    original = schema_fingerprint(config)
    config.questions[0].weights = [1, 9]
    config.count = 10
    assert schema_fingerprint(config) == original
    config.questions[0].options.append("C")
    assert schema_fingerprint(config) != original


def test_instruction_blocks_and_section_descriptions_preserve_order_and_context():
    schema = parse_form_html(
        document(
            [
                [100, "Read first", "Only answer for your current household.", 6],
                item(1, "Choose", 2, ["A", "B"]),
                [101, "Employment", "Answer about your main job.", 8],
                item(2, "Job", 0),
                [102, "End note", "Do not include personal identifiers.", 6],
            ]
        ),
        URL,
    )
    assert schema.instructions == [
        {
            "page": 1,
            "before_question": 1,
            "title": "Read first",
            "description": "Only answer for your current household.",
        },
        {
            "page": 2,
            "before_question": 2,
            "title": "Employment",
            "description": "Answer about your main job.",
        },
        {
            "page": 2,
            "before_question": 3,
            "title": "End note",
            "description": "Do not include personal identifiers.",
        },
    ]
    config = config_from_schema(schema)
    assert config.instructions == schema.instructions
    fingerprint = schema_fingerprint(config)
    config.instructions = []
    assert schema_fingerprint(config) == fingerprint


def test_sign_in_redirect_is_reported_as_access_requirement(monkeypatch):
    from gformbot import inspector

    class Response:
        url = "https://accounts.google.com/ServiceLogin"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(inspector, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="sign-in or access permission"):
        inspector.inspect_form(URL)


def policy_document(*, one_response=None, email=1, signin=False):
    data = json.loads(document([item(1, "Name", 0)]).split(" = ", 1)[1].removesuffix(";</script>"))
    data[1].extend([None] * (11 - len(data[1])))
    data[1][10] = [None, one_response, None, None, None, None, email]
    data.extend([None] * (19 - len(data)))
    data[18] = signin
    return "<script>FB_PUBLIC_LOAD_DATA_ = " + json.dumps(data) + ";</script>"


@pytest.mark.parametrize(
    ("once", "email", "signin", "expected_once", "collection", "required"),
    [
        (None, 1, False, False, "none", False),
        (1, 1, False, True, "none", True),
        (0, 2, False, False, "verified", True),
        (0, 3, False, False, "input", False),
        (0, 1, True, False, "none", True),
        ("changed", 9, False, None, "unknown", False),
    ],
)
def test_observed_access_policy(once, email, signin, expected_once, collection, required):
    schema = parse_form_html(policy_document(one_response=once, email=email, signin=signin), URL)
    assert schema.one_response is expected_once
    assert schema.email_collection == collection
    assert schema.requires_signin is required


def test_missing_policy_is_unknown_without_changing_the_fingerprint():
    schema = parse_form_html(document([item(1, "Name", 0)]), URL)
    assert schema.one_response is None
    assert schema.email_collection == "unknown"
    before = schema_fingerprint(schema)
    schema.one_response = True
    schema.requires_signin = True
    schema.email_collection = "verified"
    assert schema_fingerprint(schema) == before


def test_responder_email_is_a_required_answer_not_a_verified_identity():
    schema = parse_form_html(policy_document(email=3), URL)
    email, name = schema.questions
    assert (email.index, email.entry_id, email.item_id) == (1, "emailAddress", "emailAddress")
    assert email.text == "Email"
    assert email.qtype == "text" and email.required
    assert email.validation == [[2, 102]]
    assert (name.index, name.entry_id) == (2, "1")
    assert not schema.requires_signin


def test_verified_email_is_not_an_answer_to_generate():
    schema = parse_form_html(policy_document(email=2), URL)
    assert len(schema.questions) == 1
    assert schema.questions[0].text == "Name"
    assert schema.requires_signin


@pytest.mark.parametrize(
    ("body", "state"),
    [
        ("<h1>You need permission</h1>", "permission_denied"),
        ("<h1>You've already responded</h1>", "already_answered"),
        ("<p>No longer accepting responses</p>", "closed"),
        ("<h1>An unknown document</h1>", "unknown"),
    ],
)
def test_inspection_reports_distinct_access_states(body, state):
    with pytest.raises(FormAccessError) as failure:
        parse_form_html(body, URL)
    assert failure.value.state == state


def test_borrowed_browser_is_used_and_never_closed(monkeypatch):
    from gformbot import inspector

    class Driver:
        current_url = URL
        page_source = policy_document(one_response=1, email=2)

        def get(self, url):
            assert url == URL

        def quit(self):
            pytest.fail("The caller owns this browser")

    monkeypatch.setattr(inspector, "urlopen", lambda *a, **k: pytest.fail("Anonymous request"))
    schema = inspector.inspect_form(URL, driver=Driver())
    assert schema.requires_signin and schema.one_response


def test_http_error_is_classified_without_printing_the_response(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from gformbot import inspector

    def fail(*a, **k):
        raise HTTPError(URL, 403, "Forbidden", {}, BytesIO(b"secret@example.test"))

    monkeypatch.setattr(inspector, "urlopen", fail)
    with pytest.raises(FormAccessError) as failure:
        inspector.inspect_form(URL)
    assert failure.value.state == "permission_denied"
    assert "secret" not in str(failure.value)
