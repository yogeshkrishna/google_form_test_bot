import json

import pytest

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


def test_unsupported_controls_and_branching_are_visible():
    branch = item(2, "Branch", 2, ["A"])
    branch[4][0][1][0][2] = 123
    form = parse_form_html(document([item(1, "Upload", 13), branch]), URL)
    assert "file upload" in form.unsupported[0]
    assert "branching" in form.unsupported[1]
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
    assert form.questions[0].qtype == "file upload"
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
