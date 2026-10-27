"""Captured Google items; mutations below are explicitly synthetic edge cases."""

import copy
import json
import os
from html import escape
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai, cli, filler
from gformbot.config import config_from_schema
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html

ITEMS = json.loads((Path(__file__).parent / "fixtures/google-inputs.json").read_text())["items"]


def parsed(name):
    return parse_form_html(document([copy.deepcopy(ITEMS[name])]), URL)


def test_captured_default_date_includes_year():
    form = parsed("date_default")
    form.require_supported()
    assert (form.questions[0].date_include_time, form.questions[0].date_include_year) == (False, True)


def test_captured_date_time_includes_year_and_time():
    form = parsed("date_time")
    form.require_supported()
    assert (form.questions[0].date_include_time, form.questions[0].date_include_year) == (True, True)


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([1], (True, True)),
        ([0], (False, True)),
        ([None, 0], (False, False)),
        ([1, 0], (True, False)),
        ([None, None], (False, True)),
    ],
)
def test_sparse_date_defaults_from_google_descriptor(flags, expected):
    item = copy.deepcopy(ITEMS["date_default"])
    item[4][0][7] = flags
    form = parse_form_html(document([item]), URL)
    form.require_supported()
    assert (form.questions[0].date_include_time, form.questions[0].date_include_year) == expected


@pytest.mark.parametrize("flags", [None, "", {}, [2], ["0", 1], [0, 1, 0]])
def test_unknown_date_shapes_are_not_guessed(flags):
    item = copy.deepcopy(ITEMS["date_default"])
    item[4][0][7] = flags
    assert parse_form_html(document([item]), URL).unsupported


def test_captured_exact_checkbox_rule_is_used_everywhere():
    form = parsed("checkbox_exact")
    form.require_supported()
    config = config_from_schema(form)
    q = config.questions[0]
    assert (q.min_select, q.max_select) == (2, 2)
    for invalid in ([], q.options[:1], q.options[:3]):
        with pytest.raises(ValueError):
            validate_answer(config, {1: invalid})
    config.count = 50
    assert all(len(row[1]) == 2 for row in prepare_responses(config))


def test_captured_exact_rule_constrains_ai_output_and_edited_profiles():
    config = config_from_schema(parsed("checkbox_exact"))
    field = ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]["1"]
    assert (field["minItems"], field["maxItems"]) == (2, 2)
    valid, errors = ai._validate_batch(config, json.dumps({"responses": [{"1": [1]}]}), 1)
    assert not valid and "at least 2" in errors[0]
    valid, errors = ai._validate_batch(config, json.dumps({"responses": [{"1": [1, 2]}]}), 1)
    assert not errors and len(valid[0][1]) == 2
    with pytest.raises(ValueError):
        validate_answer(config, {1: config.questions[0].options[:1]}, partial=True)


@pytest.mark.parametrize("code,minimum,maximum", [(200, 2, None), (201, 1, 2), (204, 2, 2)])
def test_checkbox_codes_from_google_descriptor(code, minimum, maximum):
    item = copy.deepcopy(ITEMS["checkbox_minimum"])
    item[4][0][4][0][1] = code
    form = parse_form_html(document([item]), URL)
    form.require_supported()
    assert (form.questions[0].min_select, form.questions[0].max_select) == (minimum, maximum)


def test_character_count_code_is_not_an_exact_checkbox_rule():
    item = copy.deepcopy(ITEMS["checkbox_exact"])
    item[4][0][4][0][1] = 202
    assert parse_form_html(document([item]), URL).unsupported


@pytest.mark.parametrize(
    "rules",
    [
        [[7, 204, ["8"]]],
        [[7, 200, ["3"]], [7, 201, ["2"]]],
        [[7, 201, ["1"]], [7, 204, ["2"]]],
        [[7, 204, ["1"]], [7, 204, ["2"]]],
    ],
)
def test_impossible_checkbox_requirements_stop_inspection(rules):
    item = copy.deepcopy(ITEMS["checkbox_exact"])
    item[4][0][4] = rules
    with pytest.raises(ValueError, match="selection"):
        parse_form_html(document([item]), URL)


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable local browser fixtures.")
def test_captured_fields_roundtrip_and_fill_without_submitting(html_page, tmp_path):
    items = [copy.deepcopy(ITEMS[name]) for name in ("date_default", "date_time", "checkbox_exact")]
    form = parse_form_html(document(items), URL)
    form.require_supported()
    config = config_from_schema(form)
    boxes = []
    for q in config.questions:
        if q.qtype == "date":
            controls = '<input type="date">'
            if q.date_include_time:
                controls += '<input aria-label="Hour" type="number"><input aria-label="Minute" type="number">'
        else:
            controls = "".join(
                f'<input type="checkbox" role="checkbox" value="{escape(label)}">' for label in q.options
            )
        boxes.append(
            f'<div role="listitem" data-item-id="{q.item_id}"><h2>{escape(q.text)}</h2>{controls}</div>'
        )
    config.url = html_page(
        "<form>"
        + "".join(boxes)
        + '<button type="button" onclick="window.submitted=true">Submit</button></form>'
    )
    row = {1: "2028-02-29", 2: "2028-02-29 00:05", 3: ["Python", "SQL"]}
    csv = write_csv(config, [row], tmp_path / "data.csv")
    assert load_reference(config, csv) == [row]
    plan = cli._save_plan(config, [row], tmp_path / "plan.json", cleanup_on_success=False)
    restored, answers = cli._load_plan(plan)
    from gformbot.browser import get_driver

    driver = get_driver("chrome", headless=True)
    try:
        assert filler.execute_response(driver, restored, answers[0], submit=False)["status"] == "filled"
        assert driver.execute_script(
            "return [...document.querySelectorAll('input[type=date]')].map(e=>e.value)"
        ) == ["2028-02-29", "2028-02-29"]
        assert driver.execute_script(
            "return [...document.querySelectorAll('input[type=number]')].map(e=>e.value)"
        ) == ["0", "5"]
        assert driver.execute_script(
            "return [...document.querySelectorAll('input[type=checkbox]:checked')].map(e=>e.value)"
        ) == ["Python", "SQL"]
        assert not driver.execute_script("return Boolean(window.submitted)")
        assert not driver._gformbot_submission_clicked
    finally:
        driver.quit()
