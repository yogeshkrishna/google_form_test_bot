import copy
import json
import os
from pathlib import Path

import pytest
from test_inspector import URL, document, item
from test_routing import branch_items

from gformbot import ai
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html, schema_fingerprint


def time_config(duration=False):
    raw = json.loads((Path(__file__).parent / "fixtures/google-time.json").read_text(encoding="utf-8"))[
        "items"
    ][0]
    raw = copy.deepcopy(raw)
    raw[4][0][6] = [1] if duration else []
    return config_from_schema(parse_form_html(document([raw]), URL))


@pytest.mark.parametrize(
    "duration,value", [(False, "00:00"), (False, "23:59"), (True, "27:15:00"), (True, "72:59:59")]
)
def test_time_pipeline_roundtrip(duration, value, tmp_path):
    config = time_config(duration)
    assert not config.unsupported
    assert config.questions[0].qtype == ("duration" if duration else "time")
    answer = {1: value}
    assert validate_answer(config, answer) == answer
    restored = config_from_dict(config_to_dict(config))
    assert schema_fingerprint(restored) == schema_fingerprint(config)
    path = tmp_path / "time.csv"
    write_csv(config, [answer], path)
    assert load_reference(config, path) == [answer]
    assert (
        ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]["1"]["type"]
        == "string"
    )


@pytest.mark.parametrize(
    "duration,value",
    [
        (False, "24:00"),
        (False, "12:60"),
        (False, "12:30:01"),
        (True, "73:00:00"),
        (True, "27:15:60"),
        (True, "-1:00:00"),
    ],
)
def test_invalid_time_fails_before_browser(duration, value):
    with pytest.raises(ValueError, match="time|duration|HH"):
        validate_answer(time_config(duration), {1: value})


@pytest.mark.parametrize(
    "value,expected", [("2:05 PM", "14:05"), ("12:00 AM", "00:00"), ("14:05:00", "14:05")]
)
def test_csv_clock_formats(value, expected, tmp_path):
    path = tmp_path / "time.csv"
    path.write_text("Q1\n" + value + "\n")
    assert load_reference(time_config(), path) == [{1: expected}]


def branch_config():
    items = branch_items()
    items[4] = item(3, "Upload", 13)
    return config_from_schema(parse_form_html(document(items), URL))


def test_unsupported_unused_branch_allowed_chosen_branch_blocked(tmp_path):
    config = branch_config()
    answer = {1: "Student", 2: "Math", 4: "Hello"}
    assert validate_answer(config, answer)[2] == "Math"
    with pytest.raises(ValueError, match="Upload|upload"):
        validate_answer(config, {1: "Worker", 3: "file", 4: "Hello"})
    path = tmp_path / "branch.csv"
    write_csv(config, [answer], path)
    assert load_reference(config, path)[0][1] == "Student"
    restored = config_from_dict(config_to_dict(config))
    assert schema_fingerprint(restored) == schema_fingerprint(config)
    config.unsupported.append("Unclear routing")
    with pytest.raises(ValueError, match="Unclear routing"):
        validate_answer(config, answer)


def test_random_avoids_unsupported_and_looping_routes():
    config = branch_config()
    config.questions[1].text_pool = ["Math"]
    config.questions[3].text_pool = ["Hello"]
    config.count = 20
    for row in prepare_responses(config):
        assert row[1] != "Worker"
    config.questions[0].fixed_value = "Worker"
    with pytest.raises(ValueError, match="route|Upload|upload"):
        prepare_responses(config)
    from gformbot.routing import Routing

    config.questions[0].fixed_value = None
    config.section_next = [2, 1, 4, 0]
    for _ in range(10):
        planned = Routing(config).choose_route({})
        assert list(Routing(config).walk(planned)) == [1]


def test_unknown_branch_rules_fail_closed_and_legacy_findings_stay_global():
    items = branch_items()
    items[0][4][0][1][0][2] = 987654321
    with pytest.raises(ValueError, match="destination"):
        parse_form_html(document(items), URL)
    config = branch_config()
    for q in config.questions:
        q.unsupported = []
    with pytest.raises(ValueError, match="Unsupported|reliably"):
        validate_answer(config, {1: "Skip"})
    items = branch_items()
    items[0][4][0][1][1][0] = "Student"
    with pytest.raises(ValueError, match="Ambiguous routing"):
        parse_form_html(document(items), URL)


def test_ai_schema_and_corrections_respect_selected_route():
    config = branch_config()
    fields = ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]
    assert fields["3"]["const"] == ""
    valid, errors = ai._validate_batch(
        config, json.dumps({"responses": [{"1": 3, "2": "", "3": "", "4": ""}]}), 1
    )
    assert not errors and valid[0][1] == "Skip"
    valid, errors = ai._validate_batch(
        config, json.dumps({"responses": [{"1": 2, "2": "", "3": "", "4": "Hi"}]}), 1
    )
    assert not valid and "Upload" in errors[0]


def test_unknown_time_settings_and_lossy_csv_rejected(tmp_path):
    raw = json.loads((Path(__file__).parent / "fixtures/google-time.json").read_text(encoding="utf-8"))[
        "items"
    ][0]
    raw[4][0][6] = [2]
    form = parse_form_html(document([raw]), URL)
    with pytest.raises(ValueError, match="time settings"):
        form.require_supported()
    path = tmp_path / "time.csv"
    path.write_text("Q1\n14:05:01\n")
    with pytest.raises(ValueError, match="CSV row 2.*seconds"):
        load_reference(time_config(), path)


def test_unsupported_route_blocks_before_opening_browser():
    from gformbot.filler import execute_response

    class Unopened:
        def get(self, url):
            pytest.fail("Unsupported route opened a browser")

    with pytest.raises(ValueError, match="Upload"):
        execute_response(Unopened(), branch_config(), {1: "Worker", 4: "Hi"})


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures.")
@pytest.mark.parametrize("duration,value", [(False, "00:05"), (True, "27:15:03")])
@pytest.mark.parametrize("google_labels", [False, True])
def test_time_browser_readback(duration, value, google_labels, html_page):
    from gformbot.browser import get_driver
    from gformbot.filler import execute_response

    config = time_config(duration)
    q = config.questions[0]
    fields = "".join(
        f'<input aria-label="{part.title()}s" type="text" role="combobox" onchange="if(this.value)this.value=this.value.padStart(2,\'0\')">'
        if google_labels
        else f'<input name="entry.{q.entry_id}_{part}" aria-label="{part.title()}" type="number">'
        for part in (["hour", "minute", "second"] if duration else ["hour", "minute"])
    )
    config.url = html_page(
        f'<div role="listitem"><div role="heading">{q.text}</div><input type="hidden" name="entry.{q.entry_id}_sentinel">{fields}</div><button>Submit</button>'
    )
    driver = get_driver("chrome", headless=True)
    try:
        execute_response(driver, config, {1: value}, submit=False, timeout=3)
        assert driver.execute_script(
            'return [...document.querySelectorAll("input:not([type=hidden])")].map(e=>Number(e.value))'
        ) == list(map(int, value.split(":")))
        q.required = False
        execute_response(driver, config, {1: ""}, submit=False, timeout=3)
        assert driver.execute_script(
            'return [...document.querySelectorAll("input:not([type=hidden])")].every(e=>e.value==="")'
        )
    finally:
        driver.quit()


@pytest.mark.parametrize(
    "requested,retained,numeric,accepted",
    [
        ("02", "2", True, True),
        ("2", "02", True, True),
        ("02", "2", False, False),
        ("0", "", True, False),
        ("2", "2x", True, False),
        ("2", "3", True, False),
    ],
)
def test_numeric_time_readback_accepts_only_equal_components(requested, retained, numeric, accepted):
    from types import SimpleNamespace

    from gformbot.filler import _set_value

    driver = SimpleNamespace(execute_script=lambda *args: retained)
    field = SimpleNamespace(get_attribute=lambda *args: retained)
    if accepted:
        _set_value(driver, field, requested, 0.01, "Changed answer", numeric=numeric)
    else:
        with pytest.raises(RuntimeError, match="Changed answer"):
            _set_value(driver, field, requested, 0.01, "Changed answer", numeric=numeric)
