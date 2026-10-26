import json

import pytest

from gformbot import ai, cli
from gformbot.artifacts import digest
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html, schema_fingerprint
from gformbot.routing import Routing
from gformbot.state import Session

URL = "https://docs.google.com/forms/d/e/branch_fixture/viewform"


def question(index, title, kind=0, options=None, required=True):
    return [index + 100, title, None, kind, [[index, options, int(required)]]]


def branch_items():
    return [
        question(1, "Respondent", 2, [["Student", None, 1000], ["Worker", None, 2000], ["Skip", None, -3]]),
        [1000, "Study", None, 8, None, -2],
        question(2, "Course"),
        [2000, "Work", None, 8, None, 3000],
        question(3, "Job"),
        [3000, "Common", None, 8, None, -2],
        question(4, "Comment"),
    ]


def schema(items=None):
    metadata = ["", branch_items() if items is None else items, ["Recorded"]] + [None] * 5 + ["Routes"]
    document = "<script>FB_PUBLIC_LOAD_DATA_=" + json.dumps([None, metadata]) + ";</script>"
    return parse_form_html(document, URL)


def test_parser_keeps_choice_destinations_and_previous_section_endings():
    form = schema()
    form.require_supported()
    assert form.questions[0].branches == {"Student": 2, "Worker": 3, "Skip": 0}
    assert form.section_ids == ["-1", "1000", "2000", "3000"]
    assert form.section_next == [2, 4, 4, 0]


@pytest.mark.parametrize(
    "row",
    [{1: "Student", 2: "Math", 4: "Hello"}, {1: "Worker", 3: "Teacher", 4: "Hello"}, {1: "Skip"}],
)
def test_only_reached_required_answers_are_required(row):
    config = config_from_schema(schema())
    result = validate_answer(config, row)
    assert all(result[index] == value for index, value in row.items())


@pytest.mark.parametrize(
    "row,path", [({1: "Student"}, [1, 2, 4]), ({1: "Worker"}, [1, 3, 4]), ({1: "Skip"}, [1])]
)
def test_routes_skip_and_rejoin(row, path):
    assert list(Routing(schema()).walk(row)) == path


def test_later_routing_choice_and_explicit_continue_override_section_default():
    items = branch_items()
    items[1][5] = 3000  # Default section 1 -> section 4.
    items.insert(1, question(5, "Override", 3, [["Continue", None, -2], ["Finish", None, -3]], False))
    routes = Routing(schema(items))
    assert list(routes.walk({1: "Worker", 2: "Continue"})) == [1, 2, 4]
    assert list(routes.walk({1: "Student", 2: "Finish"})) == [1]
    assert list(routes.walk({1: "Worker", 2: ""})) == [1, 3, 4]
    assert list(routes.walk({1: "Skip", 2: "Continue"})) == [1, 2, 4]


def test_optional_blank_routing_uses_section_default():
    items = branch_items()
    items[0][4][0][2] = 0
    items[1][5] = 2000
    config = config_from_schema(schema(items))
    assert list(Routing(config).walk({1: ""})) == [1, 3, 4]
    assert validate_answer(config, {3: "Teacher", 4: "Hi"})[1] == ""


@pytest.mark.parametrize("destination", [-4, 99999, True, "1000", {}, []])
def test_unknown_destinations_are_rejected(destination):
    items = branch_items()
    items[0][4][0][1][0][2] = destination
    with pytest.raises(ValueError, match="section destination"):
        schema(items)


def test_backwards_jump_is_valid_until_it_repeats_a_section():
    items = branch_items()
    items[0][4][0][1][0][2] = 2000  # Start at section 3.
    items[5][5] = 1000  # Section 3 -> section 2 -> section 4.
    assert list(Routing(schema(items)).walk({1: "Student"})) == [1, 3, 2, 4]
    items[3][5] = -1  # Section 2 -> first section.
    with pytest.raises(ValueError, match="repeated sections"):
        list(Routing(schema(items)).walk({1: "Student"}))
    assert list(Routing(schema(items)).walk({1: "Skip"})) == [1]


def test_last_physical_section_ignores_choice_navigation():
    form = schema([question(1, "Only", 2, [["Restart", None, -1]])])
    assert list(Routing(form).walk({1: "Restart"})) == [1]
    assert validate_answer(config_from_schema(form), {1: "Restart"}) == {1: "Restart"}


def test_required_and_invalid_answers_on_the_active_route_still_fail():
    config = config_from_schema(schema())
    for row in ({1: "Student", 4: "Hi"}, {1: "Worker", 3: "Job"}, {1: "Unknown"}, {}):
        with pytest.raises(ValueError):
            validate_answer(config, row)
    with pytest.raises(ValueError, match="duplicate response keys"):
        validate_answer(config, {1: "Skip", "1": "Skip"})
    with pytest.raises(ValueError, match="unknown question"):
        validate_answer(config, {1: "Skip", 99: "Oops"})


def test_csv_allows_missing_inactive_columns_but_preserves_full_profiles(tmp_path):
    config = config_from_schema(schema())
    csv = tmp_path / "input.csv"
    original = "Respondent,Course,Comment\nStudent,Math,Hi\nSkip,unused,retained\n"
    csv.write_text(original)
    rows = load_reference(config, csv)
    assert rows[0] == {1: "Student", 2: "Math", 3: "", 4: "Hi"}
    assert rows[1] == {1: "Skip", 2: "unused", 3: "", 4: "retained"}
    assert csv.read_text() == original
    config.count = 2
    assert prepare_responses(config, rows, ordered=True) == rows
    output = tmp_path / "export.csv"
    write_csv(config, rows, output)
    assert load_reference(config, output) == rows
    csv.write_text("Respondent,Course,Comment\nWorker,Math,Hi\n")
    with pytest.raises(ValueError, match="CSV row 2.*Job"):
        load_reference(config, csv)


def test_only_active_csv_dates_and_checkbox_values_are_normalized(tmp_path):
    items = branch_items()
    items[4] = question(3, "Date", 9)
    items[4][4][0] += [None] * 4 + [[0, 1]]
    items.insert(5, question(5, "Choices", 4, [["A"], ["B"]]))
    config = config_from_schema(schema(items))
    csv = tmp_path / "dates.csv"
    csv.write_text(
        "Respondent,Course,Date,Choices,Comment\nStudent,Math,irrelevant,obsolete,Hi\nWorker,,12/31/2026,A,Hi\n"
    )
    rows = load_reference(config, csv)
    assert rows[0][3] == "irrelevant" and rows[0][4] == "obsolete"
    assert rows[1][3] == "2026-12-31" and rows[1][4] == ["A"]


def test_random_generation_only_requires_reachable_written_answers():
    config = config_from_schema(schema())
    config.questions[0].fixed_value = "Skip"
    assert prepare_responses(config) == [{1: "Skip", 2: "", 3: "", 4: ""}]
    config.questions[0].fixed_value = "Student"
    config.questions[1].text_pool = ["Math"]
    config.questions[3].fixed_value = "Hi"
    assert prepare_responses(config) == [{1: "Student", 2: "Math", 3: "", 4: "Hi"}]


def test_ai_accepts_inactive_blanks_but_rejects_missing_active_answers():
    config = config_from_schema(schema())
    content = json.dumps(
        {"responses": [{"1": 1, "2": "Math", "3": "", "4": "Hi"}, {"1": 3, "2": "", "3": "", "4": ""}]}
    )
    rows, errors = ai._validate_batch(config, content, 2)
    assert errors == [] and len(rows) == 2
    invalid = json.dumps({"responses": [{"1": 2, "2": "", "3": "", "4": "Hi"}]})
    assert "Job" in ai._validate_batch(config, invalid, 1)[1][0]
    fields = ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]
    assert fields["2"]["minLength"] == 0


def test_routing_round_trip_and_fingerprint_keep_linear_legacy_compatibility():
    config = config_from_schema(schema())
    assert config_from_dict(config_to_dict(config)) == config
    assert schema_fingerprint(config) == schema_fingerprint(schema())
    before = schema_fingerprint(config)
    config.questions[0].branches["Student"] = 3
    assert schema_fingerprint(config) != before
    linear = config_from_schema(schema([question(1, "Answer")]))
    legacy = config_to_dict(linear)
    legacy.pop("section_ids")
    legacy.pop("section_next")
    legacy["questions"][0].pop("branches")
    assert schema_fingerprint(config_from_dict(legacy)) == schema_fingerprint(linear)


def test_saved_branching_plan_resumes_original_rows_without_rewriting_answers(tmp_path):
    config = config_from_schema(schema())
    config.count = 2
    rows = [validate_answer(config, {1: "Skip"}), validate_answer(config, {1: "Student", 2: "Math", 4: "Hi"})]
    path = cli._save_plan(config, rows, tmp_path / "responses.json")
    payload = json.loads(path.read_text())
    current = Session(path, payload, digest(path))
    current.before_submit(1)
    current.confirm(1)
    restored, restored_rows = cli._load_plan(path)
    remaining = cli._track_plan(restored, restored_rows, path, digest(path), interactive=False)
    assert remaining == [rows[1]]
    assert restored._session.ids == [2]
    assert json.loads(path.read_text())["responses"] == json.loads(json.dumps(rows))


def test_editing_a_routing_answer_collects_newly_required_answers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = config_from_schema(schema())
    original = validate_answer(config, {1: "Student", 2: "Math", 4: "Hi"})
    path = cli._save_plan(config, [original])
    original_digest = digest(path)
    selections = iter([1, 1])
    text = iter(["Worker", "Teacher"])
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(cli, "_ask_text", lambda *args, **kwargs: next(text))
    _, rows, edited = cli._edit_batch(config, [original], path)
    assert rows == [{1: "Worker", 2: "Math", 3: "Teacher", 4: "Hi"}]
    assert edited != path and digest(path) == original_digest


def test_ai_generates_a_branching_batch_in_one_request(monkeypatch):
    config = config_from_schema(schema())
    requests = []

    class Driver:
        def quit(self):
            pass

    def chat(driver, messages, output_schema, *args, **kwargs):
        requests.append(json.loads(messages[1]["content"]))
        return json.dumps(
            {"responses": [{"1": 1, "2": "Math", "3": "", "4": "Hi"}, {"1": 3, "2": "", "3": "", "4": ""}]}
        )

    monkeypatch.setattr(ai, "_open_chat", lambda *args: Driver())
    monkeypatch.setattr(ai, "_chat", chat)
    rows = ai.generate_responses(config, 2)
    assert len(requests) == 1 and len(rows) == 2
    assert requests[0]["routing"] == {
        "section_next": [2, 4, 4, 0],
        "choices": {"1": {"1": 2, "2": 3, "3": 0}},
    }


def test_ai_inactive_choice_blanks_and_presets_are_supported():
    items = branch_items()
    items[2] = question(2, "Course", 2, [["Math"], ["Art"]])
    config = config_from_schema(schema(items))
    row = {"1": 3, "2": 0, "3": "", "4": ""}
    profiles, errors = ai._validate_batch(config, json.dumps({"responses": [row]}), 1, [{1: "Skip"}])
    assert errors == [] and profiles[0][2] == ""
    assert 0 in ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]["2"]["enum"]


def test_inactive_reference_answers_do_not_change_choice_frequencies():
    from gformbot.generator import apply_reference_weights

    items = branch_items()
    items[2] = question(2, "Course", 2, [["Math"], ["Art"]])
    config = config_from_schema(schema(items))
    rows = [
        validate_answer(config, {1: "Student", 2: "Math", 4: "Hi"}),
        validate_answer(config, {1: "Skip", 2: "Art"}),
    ]
    apply_reference_weights(config, rows)
    assert config.questions[1].weights == [1, 0]
    context = ai._reference_context(config, rows)
    assert context["choice_counts"]["2"] == {"1": 1, "2": 0}
    assert context["example_profiles"][1] == {"1": 3}


@pytest.mark.parametrize(
    "field,value",
    [
        ("section_ids", ["same"] * 4),
        ("section_ids", ["short"]),
        ("section_next", [2, 3, 99, 0]),
        ("section_next", [2, 3, 4, 1]),
        ("section_next", None),
    ],
)
def test_saved_config_rejects_malformed_routing(field, value):
    data = config_to_dict(config_from_schema(schema()))
    data[field] = value
    with pytest.raises(ValueError, match="Section"):
        config_from_dict(data)


def test_loop_is_rejected_before_browser_navigation():
    from gformbot.filler import execute_response

    config = config_from_schema(schema())
    config.questions[0].branches["Student"] = 1

    class Driver:
        def get(self, url):
            raise AssertionError("A looping plan must be rejected before navigation")

    with pytest.raises(ValueError, match="repeated sections"):
        execute_response(Driver(), config, {1: "Student"}, submit=True)
