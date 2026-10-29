"""Real metadata regressions and synthetic boundaries for the same rule families."""

import copy
import json
import os
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai, cli
from gformbot.config import QuestionConfig, RunConfig, validate_config
from gformbot.generator import load_reference, validate_answer, write_csv
from gformbot.inspector import parse_form_html
from gformbot.validation import text_rules

ITEMS = json.loads((Path(__file__).parent / "fixtures/google-rules.json").read_text())["items"]


def config(rule, *, required=True, kind="text"):
    return RunConfig(URL, questions=[QuestionConfig(1, "Answer", kind, required, validation=[rule])])


def test_captured_stress_rules_are_supported():
    parse_form_html(document(ITEMS), URL).require_supported()


@pytest.mark.parametrize("raw", [False, {}, 0, ""])
def test_malformed_validation_container_is_not_treated_as_no_rules(raw):
    item = copy.deepcopy(ITEMS[0])
    item[4][0][4] = raw
    with pytest.raises(ValueError, match="validation"):
        parse_form_html(document([item]), URL)


def test_invalid_answers_stop_before_browser_navigation():
    from types import SimpleNamespace

    from gformbot.filler import execute_response

    driver = SimpleNamespace(get=lambda *a: pytest.fail("Invalid answers must not reach the browser"))
    with pytest.raises(ValueError, match="Number between"):
        execute_response(driver, config([1, 7, ["0", "40"]]), {1: "41"})


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


@pytest.mark.parametrize(
    "pattern", [r"a{,2}", r"(?P<name>a)", r"(?<=a)b", r"(a)\1", r"a++", r"[\S]", r"[]a]"]
)
def test_unverified_or_python_specific_regex_syntax_fails_closed(pattern):
    with pytest.raises(ValueError):
        validate_config(config([4, 301, [pattern]]))


def test_worker_disconnect_fails_explicitly_and_recovers():
    from gformbot import patterns

    normalized = text_rules(config([4, 301, ["disconnect-test"]]).questions[0])[0]
    patterns._WORKER.process.kill()
    patterns._WORKER.process.wait()
    with pytest.raises(ValueError):
        patterns.matches(normalized.compiled, "disconnect-test")
    assert patterns.matches(normalized.compiled, "disconnect-test")


@pytest.mark.parametrize(
    "rule,good,bad",
    [
        ([1, 7, ["0", "40"]], "40", "41"),
        ([6, 202, ["2"]], "😀", "😀a"),
        ([4, 301, ["STU-[0-9]{3}"]], "STU-123", "invalid"),
    ],
)
@pytest.mark.parametrize("command", ["prepare", "generate"])
def test_public_commands_enforce_supplied_rules(tmp_path, monkeypatch, rule, good, bad, command):
    from click.testing import CliRunner

    monkeypatch.chdir(tmp_path)
    c = config(rule)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: c)
    monkeypatch.setattr(cli, "_run_fill", lambda config, **kw: False)
    csv = tmp_path / "data.csv"
    csv.write_text("Q1\n" + good + "\n", encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli.cli, [command, URL, "--data", str(csv)]).exit_code == 0
    csv.write_text("Q1\n" + bad + "\n", encoding="utf-8")
    before = csv.read_bytes()
    result = runner.invoke(cli.cli, [command, URL, "--data", str(csv)])
    assert result.exit_code != 0 and csv.read_bytes() == before


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable local browser fixtures.")
def test_supported_regex_semantics_match_google_javascript_wrappers(html_page):
    from gformbot.browser import get_driver
    from gformbot.patterns import matches

    patterns = [
        "cat",
        "cat|dog",
        "(?:cat|dog)",
        r"\d+",
        r"\w+",
        r"\bcat\b",
        r"\s",
        r"\S",
        r"[^a]",
        r"[A-Z\s]+",
        r"a.*b",
        r"^a$",
        r"\u00e9",
        r"\x61",
        r"[0-9]{2,3}",
        r"a+?",
        r"a(?=b)",
        r"a(?!b)",
        "😀",
        ".",
    ]
    values = [
        "cat",
        "a cat!",
        "dog",
        "cat\n",
        "a\r\nb",
        "ab",
        "aaa",
        "abc",
        "١٢",
        "12",
        "é",
        "😀",
        "\ufeff",
        "\u00a0",
        "\u2028",
        " ",
        "A B",
    ]
    cases = []
    compiled = []
    for op in (299, 300, 301, 302):
        for pattern in patterns:
            rule = text_rules(config([4, op, [pattern]]).questions[0])[0]
            for value in values:
                cases.append([rule.pattern, value])
                compiled.append((rule.compiled, value))
    driver = get_driver("chrome")
    try:
        driver.get(html_page("<!doctype html><html><body>Regex conformance fixture</body></html>"))
        expected = driver.execute_script(
            "return arguments[0].map(([pattern,value])=>new RegExp(pattern).test(value))", cases
        )
        actual = [matches(pattern, value) for pattern, value in compiled]
        assert actual == expected, [
            (case, a, e) for case, a, e in zip(cases, actual, expected, strict=True) if a != e
        ][:5]
    finally:
        driver.quit()
