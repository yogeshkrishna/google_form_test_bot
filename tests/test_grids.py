import copy
import json
import os
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot.config import config_from_dict, config_from_schema, config_to_dict, validate_config
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html, schema_fingerprint


def grids(checkbox=False, unique=False, required=True):
    items = json.loads((Path(__file__).parent / "fixtures/google-grids.json").read_text(encoding="utf-8"))["items"]
    item = copy.deepcopy(items[2 if checkbox else 0])
    for row in item[4]:
        row[2] = int(required)
    if unique:
        item[8] = [[8, 205]]
    return config_from_schema(parse_form_html(document([item]), URL))


@pytest.mark.parametrize("checkbox", [False, True])
def test_real_grid_metadata_and_saved_roundtrip(checkbox):
    config = grids(checkbox)
    assert not config.unsupported
    assert len(config.questions) == 3
    assert len({q.entry_id for q in config.questions}) == 3
    assert len({q.item_id for q in config.questions}) == 1
    assert {q.qtype for q in config.questions} == {"checkbox" if checkbox else "radio"}
    assert all(q.grid_title and q.grid_row and q.required for q in config.questions)
    restored = config_from_dict(config_to_dict(config))
    assert schema_fingerprint(restored) == schema_fingerprint(config)


@pytest.mark.parametrize("checkbox", [False, True])
def test_grid_csv_and_json_exact_roundtrip(checkbox, tmp_path):
    config = grids(checkbox)
    answer = {q.index: q.options[:2] if checkbox else q.options[0] for q in config.questions}
    path = tmp_path / "export.csv"
    write_csv(config, [answer], path)
    assert load_reference(config, path) == [answer]
    path.write_text(
        ",".join(q.text for q in config.questions) + "\n" + ",".join(q.options[0] for q in config.questions),
        encoding="utf-8",
    )
    assert load_reference(config, path) == [
        {q.index: [q.options[0]] if checkbox else q.options[0] for q in config.questions}
    ]
    assert validate_answer(config, json.loads(json.dumps(answer))) == answer


@pytest.mark.parametrize("checkbox", [False, True])
def test_grid_column_uniqueness_required_rows_and_random(checkbox):
    config = grids(checkbox, unique=True)
    answer = {q.index: [q.options[0]] if checkbox else q.options[0] for q in config.questions}
    with pytest.raises(ValueError, match="column"):
        validate_answer(config, answer)
    with pytest.raises(ValueError, match="required|at least"):
        validate_answer(config, {})
    config.count = 30
    for profile in prepare_responses(config):
        assert validate_answer(config, profile) == profile
        values = [v for value in profile.values() for v in (value if isinstance(value, list) else [value])]
        assert len(values) == len(set(values)) == 3
    config.questions[0].fixed_value = [config.questions[0].options[0]] if checkbox else config.questions[0].options[0]
    config.count = 1
    assert prepare_responses(config)[0][1] == config.questions[0].fixed_value


def test_impossible_and_inconsistent_grids_rejected():
    config = grids(unique=True)
    for q in config.questions:
        q.options = q.options[:2]
    with pytest.raises(ValueError, match="grid|Grid"):
        validate_config(config)
    config = grids()
    config.questions[1].options.reverse()
    with pytest.raises(ValueError, match="grid|Grid"):
        validate_config(config)


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures.")
@pytest.mark.parametrize("checkbox", [False, True])
def test_shuffled_grid_fill_and_readback(checkbox, html_page):
    from gformbot.browser import get_driver
    from gformbot.filler import execute_response

    config = grids(checkbox)
    role = "checkbox" if checkbox else "radio"
    body = ""
    for q in reversed(config.questions):
        body += f'<div role="radiogroup"><span>{q.grid_row}</span>'
        body += f'<input type="hidden" name="entry.{q.entry_id}_sentinel">'
        for option in q.options:
            body += f'<input type="{role}" name="entry.{q.entry_id}" value="{option}">'
        body += "</div>"
    config.url = html_page(
        f'<div role="listitem" data-item-id="{config.questions[0].item_id}">'
        f'<div role="heading">{config.questions[0].grid_title}</div>{body}</div><button>Submit</button>'
    )
    answer = {q.index: q.options[:2] if checkbox else q.options[0] for q in config.questions}
    driver = get_driver("chrome", headless=True)
    try:
        execute_response(driver, config, answer, submit=False, timeout=3)
        for q in config.questions:
            selected = driver.execute_script(
                "return [...document.getElementsByName(arguments[0])].filter(e=>e.checked).map(e=>e.value)",
                "entry." + q.entry_id,
            )
            assert selected == (answer[q.index] if checkbox else [answer[q.index]])
    finally:
        driver.quit()
