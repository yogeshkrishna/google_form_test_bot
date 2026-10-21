import csv
import random

import pytest

from gformbot.config import (
    QuestionConfig,
    RunConfig,
    config_from_dict,
    config_to_dict,
    load_config,
    save_config,
    validate_config,
)
from gformbot.generator import (
    apply_reference_weights,
    load_reference,
    make_answer,
    map_columns,
    mapping_diagnostics,
    parse_checkbox,
    prepare_responses,
    read_pool,
    validate_answer,
    write_csv,
)


def config(**kwargs):
    return RunConfig(
        url="https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Activity", "radio", True, ["Study", "Work"], entry_id="11"),
            QuestionConfig(
                2, "Places", "checkbox", True, ["Roads, footpaths", "Parks", "None"], entry_id="12"
            ),
            QuestionConfig(3, "Comment", "text", False, entry_id="13"),
        ],
        **kwargs,
    )


def write_pool(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def test_comma_labels_and_ordinary_last_option_are_preserved():
    c = config()
    assert parse_checkbox(c.questions[1], "Roads, footpaths, None") == ["Roads, footpaths", "None"]
    assert validate_answer(c, {1: "Study", 2: ["Parks", "None"]})[2] == ["Parks", "None"]


def test_ambiguous_checkbox_requires_json():
    q = QuestionConfig(1, "Choices", "checkbox", options=["A", "B", "A,B"])
    with pytest.raises(ValueError, match="ambiguous"):
        parse_checkbox(q, "A,B")
    assert parse_checkbox(q, '["A,B"]') == ["A,B"]


def test_required_blank_never_becomes_random():
    with pytest.raises(ValueError, match="blank"):
        validate_answer(config(), {1: "", 2: ["Parks"]})


def test_optional_blanks_remain_blank():
    c = config()
    c.questions[1].required = False
    assert validate_answer(c, {1: "Study", 2: "", 3: ""}) == {1: "Study", 2: [], 3: ""}


def test_complete_blank_optional_csv_row_is_preserved(tmp_path):
    c = config()
    for q in c.questions:
        q.required = False
    path = write_pool(tmp_path / "blank.csv", ["Q1", "Q2", "Q3"], [["", "", ""]])
    assert load_reference(c, path) == [{1: "", 2: [], 3: ""}]


def test_weighted_checkbox_sampling_has_no_duplicates():
    c = config(count=100)
    q = c.questions[1]
    q.min_select = q.max_select = 2
    q.weights = [1, 10, 0]
    for row in prepare_responses(c):
        assert set(row[2]) == {"Roads, footpaths", "Parks"}


def test_exclusive_and_count_repairs_are_explicit():
    c = config()
    c.questions[1].exclusive_options = ["None"]
    with pytest.raises(ValueError, match="alone"):
        validate_answer(c, {1: "Work", 2: ["Parks", "None"]})
    changes = []
    assert validate_answer(c, {1: "Work", 2: ["Parks", "None"]}, repair=True, changes=changes)[2] == ["None"]
    assert changes
    with pytest.raises(ValueError, match="unknown"):
        validate_answer(c, {1: "Work", 2: ["Not an option"]}, repair=True)


def test_entire_source_is_validated_before_sampling(tmp_path):
    path = write_pool(tmp_path / "pool.csv", ["Q1", "Q2"], [["Work", "Parks"], ["Invalid", "Parks"]])
    with pytest.raises(ValueError, match="CSV row 3"):
        load_reference(config(), path)


def test_whole_rows_keep_relationships_and_order(tmp_path):
    path = write_pool(
        tmp_path / "pool.csv", ["Q1", "Q2", "Q3"], [["Study", "Parks", "student"], ["Work", "None", "worker"]]
    )
    c = config(count=20)
    profiles = load_reference(c, path)
    answers = prepare_responses(c, profiles)
    assert all(row in profiles for row in answers)
    c.count = 2
    assert prepare_responses(c, profiles, ordered=True) == profiles
    c.count = 3
    with pytest.raises(ValueError, match="Ordered"):
        prepare_responses(c, profiles, ordered=True)


def test_missing_and_ambiguous_columns_do_not_use_prefix_matching():
    c = config()
    with pytest.raises(ValueError, match="missing required"):
        map_columns(c, ["Activity and something else", "Q2"])
    with pytest.raises(ValueError, match="multiple"):
        map_columns(c, ["Activity", "Q1", "Q2"])
    assert map_columns(c, ["Activity", "Q1", "Q2"], {1: "Q1", 2: "Q2"})[1] == "Q1"


def test_fixed_value_does_not_replace_imported_data():
    c = config()
    c.questions[0].fixed_value = "Study"
    assert make_answer(c, {"Q1": "Work", "Q2": "Parks"})[1] == "Work"


def test_file_changes_are_not_hidden_by_a_stale_cache(tmp_path):
    path = write_pool(tmp_path / "pool.csv", ["Q1", "Q2"], [["Study", "Parks"]])
    assert load_reference(config(), path)[0][1] == "Study"
    write_pool(path, ["Q1", "Q2"], [["Work", "None"]])
    assert load_reference(config(), path)[0][1] == "Work"


@pytest.mark.parametrize(
    "rows",
    [
        (["Q1", "Q1"], [["Study", "Work"]]),
        (["Q1", "Q2"], [["Study"]]),
        (["Q1", "Q2"], [["Study", "Parks", "extra"]]),
    ],
)
def test_malformed_csv_rejected(tmp_path, rows):
    path = write_pool(tmp_path / "bad.csv", *rows)
    with pytest.raises(ValueError):
        read_pool(path)


def test_generated_csv_roundtrips_unicode_and_checkbox_json(tmp_path):
    c = config()
    row = {1: "Study", 2: ["Roads, footpaths", "Parks"], 3: 'Shade, "trees" 🌳'}
    path = write_csv(c, [row], tmp_path / "out.csv")
    assert load_reference(c, path) == [row]


def test_source_csv_cannot_be_overwritten(tmp_path):
    path = write_pool(tmp_path / "source.csv", ["Q1", "Q2"], [["Study", "Parks"]])
    c = config(pool_file=str(path))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        write_csv(c, [{1: "Work", 2: ["None"]}], path)
    assert path.read_bytes() == before


def test_reference_weights_use_full_source_counts():
    c = config()
    rows = [{1: "Study", 2: ["Parks"], 3: ""}] * 3 + [{1: "Work", 2: ["None"], 3: ""}]
    apply_reference_weights(c, rows)
    assert c.questions[0].weights == [3, 1]
    assert c.questions[1].weights == [0, 3, 1]


def test_config_roundtrip_preserves_inspected_fields_and_mapping(tmp_path):
    c = config()
    q = c.questions[1]
    q.description = "Choose at most two"
    q.item_id = "22"
    q.min_select, q.max_select = 1, 2
    q.exclusive_options, q.validation = ["None"], [[7, 201, [2]]]
    c.pool_column_map = {1: "Activity", 2: "Places", 3: None}
    c.sections, c.form_description, c.confirmation_text = ["A"], "Survey", "Thanks!"
    path = tmp_path / "config.yaml"
    save_config(c, path)
    restored = load_config(path)
    assert restored.questions == c.questions
    assert restored.pool_column_map == c.pool_column_map
    assert restored.sections == ["A"]
    assert restored.confirmation_text == "Thanks!"


def test_relative_pool_path_resolves_against_config_directory(tmp_path, monkeypatch):
    c = config(pool_file="pool.csv")
    save_config(c, tmp_path / "config.yaml")
    monkeypatch.chdir(tmp_path.parent)
    assert load_config(tmp_path / "config.yaml").pool_file == str(tmp_path / "pool.csv")


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("count", 0),
        ("count", 501),
        ("count", True),
        ("max_delay", float("nan")),
        ("min_delay", -1),
        ("max_delay", 0),
        ("headless", "false"),
        ("browser", "bad"),
    ],
)
def test_bad_run_settings_fail_early(attribute, value):
    c = config()
    setattr(c, attribute, value)
    with pytest.raises(ValueError):
        validate_config(c)


@pytest.mark.parametrize("weights", [[0, 0], [-1, 1], [float("inf"), 1], [1], [True, 1]])
def test_invalid_weights_fail_early(weights):
    c = config()
    c.questions[0].weights = weights
    with pytest.raises(ValueError, match="weights"):
        validate_config(c)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("options", "AB"),
        ("options", [1, 2]),
        ("options", [{"A": 1}]),
        ("required", "false"),
        ("page", True),
        ("entry_id", 123),
        ("exclusive_options", ["Missing"]),
        ("weights", "11"),
        ("text_pool", "words"),
        ("validation", "email"),
    ],
)
def test_malformed_question_shapes_are_rejected_before_execution(attribute, value):
    c = config()
    setattr(c.questions[0], attribute, value)
    with pytest.raises(ValueError, match="Q1"):
        validate_config(c)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("questions", {}),
        ("sections", "A"),
        ("unsupported", "bad"),
        ("pool_column_map", []),
        ("browser", []),
        ("log_file", None),
        ("instructions", [{"title": "Missing fields"}]),
    ],
)
def test_malformed_root_shapes_have_actionable_errors(attribute, value):
    c = config()
    setattr(c, attribute, value)
    with pytest.raises(ValueError):
        validate_config(c)


def test_invalid_fixed_values_and_text_pool_fail_during_config_validation():
    c = config()
    c.questions[0].fixed_value = "Unknown"
    with pytest.raises(ValueError, match="exact option"):
        validate_config(c)
    c.questions[0].fixed_value = None
    c.questions[2].text_pool = ["first\nsecond"]
    with pytest.raises(ValueError, match="single-line"):
        validate_config(c)


@pytest.mark.parametrize("value", ["first\nsecond", "first\rsecond", "first\r\nsecond"])
def test_single_line_text_rejects_line_breaks_before_browser(value):
    c = config()
    with pytest.raises(ValueError, match="single-line"):
        validate_answer(c, {1: "Study", 2: ["Parks"], 3: value})
    c.questions[2].qtype = "textarea"
    assert validate_answer(c, {1: "Study", 2: ["Parks"], 3: value})[3] == value


def test_mapping_diagnostics_exposes_dropped_optional_data(tmp_path):
    c = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Name", "text"),
            QuestionConfig(2, "Cake", "radio", options=["Chocolate"]),
        ],
    )
    report = mapping_diagnostics(c, ["Full name", "Flavour"])
    assert report == {
        "mapping": {1: None, 2: None},
        "unmatched_questions": [1, 2],
        "unused_columns": ["Full name", "Flavour"],
        "matched_count": 0,
    }
    path = write_pool(tmp_path / "mismatch.csv", ["Full name", "Flavour"], [["Alice", "Chocolate"]])
    with pytest.raises(ValueError, match="No CSV columns match"):
        load_reference(c, path)
    assert load_reference(c, path, mapping={1: None, 2: None}) == [{1: "", 2: ""}]


def test_shuffle_chooses_distinct_complete_source_profiles():
    c = config(count=2)
    rows = [{1: "Study", 2: ["Parks"], 3: "student"}, {1: "Work", 2: ["None"], 3: "worker"}]
    for _ in range(10):
        shuffled = prepare_responses(c, rows, sampling="shuffle")
        assert len(shuffled) == 2 and shuffled[0] != shuffled[1]
        assert all(row in rows for row in shuffled)
    c.count = 3
    with pytest.raises(ValueError, match="Shuffle"):
        prepare_responses(c, rows, sampling="shuffle")
    assert len(prepare_responses(c, rows, sampling="sample")) == 3


def test_legacy_inert_page_delays_load_but_are_not_reserialized():
    raw = config_to_dict(config())
    raw["run_settings"].update(page_delay_min=1, page_delay_max=2)
    restored = config_from_dict(raw)
    assert restored.page_delay_min == 1
    updated = config_to_dict(restored)
    assert "page_delay_min" not in updated and "page_delay_min" not in updated["run_settings"]
    assert "page_delay_max" not in updated["run_settings"]


def test_unknown_configuration_keys_are_named():
    raw = config_to_dict(config())
    raw["typo"] = True
    with pytest.raises(ValueError, match="typo"):
        config_from_dict(raw)


def test_long_checkbox_csv_cell_does_not_depend_on_recursion_depth():
    q = QuestionConfig(1, "Choices", "checkbox", options=["A"])
    assert parse_checkbox(q, ", ".join(["A"] * 1500)) == ["A"] * 1500


def test_many_ambiguous_checkbox_paths_are_bounded_and_require_json():
    q = QuestionConfig(1, "Choices", "checkbox", options=["A", "A,A"])
    with pytest.raises(ValueError, match="ambiguous"):
        parse_checkbox(q, ",".join(["A"] * 1000))


def test_random_checkbox_can_reach_all_legal_counts(monkeypatch):
    from gformbot.generator import _random_from_options

    q = QuestionConfig(1, "Choices", "checkbox", options=[str(i) for i in range(8)])
    monkeypatch.setattr(random, "choice", max)
    assert set(_random_from_options(q)) == set(q.options)
    q.max_select = 4
    assert len(_random_from_options(q)) == 4
    q.max_select = None
    q.exclusive_options = ["7"]
    assert set(_random_from_options(q)) == set(q.options[:-1])


def test_repeated_profiles_have_independent_checkbox_arrays():
    c = config(count=2)
    rows = [{1: "Study", 2: ["Parks"], 3: "student"}]
    answers = prepare_responses(c, rows)
    answers[0][2].append("None")
    assert answers[1][2] == rows[0][2] == ["Parks"]


def test_duplicate_integer_string_mapping_keys_are_rejected():
    with pytest.raises(ValueError, match="duplicate column mapping"):
        map_columns(config(), ["Q1", "Q2", "Other"], {1: "Q1", "1": "Other", 2: "Q2"})
