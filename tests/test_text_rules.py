"""Real metadata regressions and synthetic boundaries for the same rule families."""

import json
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai, cli
from gformbot.config import QuestionConfig, RunConfig, validate_config
from gformbot.generator import load_reference, validate_answer, write_csv
from gformbot.inspector import parse_form_html

ITEMS = json.loads((Path(__file__).parent / "fixtures/google-rules.json").read_text())["items"]


def config(rule, *, required=True, kind="text"):
    return RunConfig(URL, questions=[QuestionConfig(1, "Answer", kind, required, validation=[rule])])


def test_captured_stress_rules_are_supported():
    parse_form_html(document(ITEMS), URL).require_supported()


@pytest.mark.parametrize(
    "code,args,good,bad",
    [
        (1, ["2"], "2.01", "2"),
        (2, ["2"], "2", "1.99"),
        (3, ["2"], "1.99", "2"),
        (4, ["2"], "2", "2.01"),
        (5, ["0.1"], "0.10", "0.10000000000000001"),
        (6, ["2"], "3", "2"),
        (7, ["0", "40"], "40", "40.01"),
        (8, ["0", "40"], "41", "40"),
        (9, [], "-1.25", "NaN"),
        (10, [], "2.0", "2.1"),
    ],
)
def test_number_boundaries_preserve_input(code, args, good, bad):
    c = config([1, code, args])
    assert validate_answer(c, {1: good}) == {1: good}
    with pytest.raises(ValueError):
        validate_answer(c, {1: bad})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1,234", "1_000", "1e99999"])
def test_invalid_numbers_are_not_coerced(value):
    with pytest.raises(ValueError):
        validate_answer(config([1, 9, []]), {1: value})


@pytest.mark.parametrize(
    "rule,good,bad",
    [
        ([2, 100, ["cat"]], "a cat!", "CAT"),
        ([2, 101, ["cat"]], "dog", "a cat"),
        ([6, 202, ["2"]], "😀", "😀a"),
        ([6, 203, ["2"]], "😀", "a"),
        ([4, 299, ["cat"]], "a cat!", "dog"),
        ([4, 300, ["cat"]], "dog", "cat"),
        ([4, 301, ["STU-[0-9]{3}"]], "STU-123", "xSTU-123"),
        ([4, 302, ["STU-[0-9]{3}"]], "OTHER", "STU-123"),
    ],
)
def test_text_length_and_pattern_rules(rule, good, bad):
    c = config(rule, kind="textarea")
    assert validate_answer(c, {1: good}) == {1: good}
    with pytest.raises(ValueError):
        validate_answer(c, {1: bad})


@pytest.mark.parametrize("rule", [[1, 7, ["0", "40"]], [6, 203, ["2"]], [4, 301, ["STU-[0-9]{3}"]]])
def test_optional_blanks_remain_blank(rule):
    assert validate_answer(config(rule, required=False), {1: ""}) == {1: ""}


@pytest.mark.parametrize(
    "rule",
    [
        [1, 7, ["40", "0"]],
        [1, 1, ["NaN"]],
        [1, 9, ["1"]],
        [6, 202, ["-1"]],
        [6, 203, [True]],
        [4, 301, ["["]],
        [4, 301, ["(?i)abc"]],
        [2, 999, []],
    ],
)
def test_invalid_rule_metadata_fails_before_generation(rule):
    with pytest.raises(ValueError):
        validate_config(config(rule))


def test_csv_json_edit_and_ai_share_validation(tmp_path):
    c = config([4, 301, ["STU-[0-9]{3}"]])
    good = {1: "STU-123"}
    csv = write_csv(c, [good], tmp_path / "input.csv")
    before = csv.read_bytes()
    assert load_reference(c, csv) == [good] and csv.read_bytes() == before
    plan = cli._save_plan(c, [good], tmp_path / "plan.json", cleanup_on_success=False)
    assert cli._load_plan(plan)[1] == [good]
    for partial in (False, True):
        with pytest.raises(ValueError):
            validate_answer(c, {1: "invalid"}, partial=partial)
    accepted, errors = ai._validate_batch(c, json.dumps({"responses": [{"1": "invalid"}]}), 1)
    assert not accepted and errors
    schema = ai._response_schema(c, 1)["properties"]["responses"]["items"]["properties"]["1"]
    assert schema["pattern"] == "^STU-[0-9]{3}$"


def test_regex_timeout_is_bounded_and_does_not_poison_following_checks():
    import time

    c = config([4, 301, ["(a+)+"]])
    started = time.monotonic()
    with pytest.raises(ValueError):
        validate_answer(c, {1: "a" * 20000 + "!"})
    assert time.monotonic() - started < 8
    assert validate_answer(config([4, 301, ["ok"]]), {1: "ok"}) == {1: "ok"}
