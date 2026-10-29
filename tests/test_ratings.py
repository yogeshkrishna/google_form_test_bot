import json
import os
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai, cli
from gformbot.artifacts import digest
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html, schema_fingerprint
from gformbot.state import Session


def rating(icon=1, required=True):
    item = json.loads((Path(__file__).parent / "fixtures/google-ratings.json").read_text(encoding="utf-8"))[
        "items"
    ][0]
    item[4][0][14] = [icon]
    item[4][0][2] = int(required)
    return config_from_schema(parse_form_html(document([item]), URL))


@pytest.mark.parametrize("icon,name", [(1, "star"), (2, "heart"), (3, "thumb_up")])
def test_rating_pipeline_and_progress(icon, name, tmp_path):
    config = rating(icon)
    assert not config.unsupported
    assert config.questions[0].qtype == "rating"
    assert config.questions[0].rating_icon == name
    assert schema_fingerprint(config_from_dict(config_to_dict(config))) == schema_fingerprint(config)
    config.count = 2
    answers = [{1: "1"}, {1: "5"}]
    path = tmp_path / "ratings.csv"
    write_csv(config, answers, path)
    assert load_reference(config, path) == answers
    assert all(row[1] in config.questions[0].options for row in prepare_responses(config))
    assert ai._decode_profile(config, {"1": 5}) == {"1": "5"}
    assert ai._encode_profile(config, {1: "5"}) == {"1": 5}
    assert ai._response_schema(config, 1)["properties"]["responses"]["items"]["properties"]["1"]["enum"] == [
        1,
        2,
        3,
        4,
        5,
    ]
    saved = cli._save_plan(config, answers, tmp_path / "ratings.json", cleanup_on_success=False)
    payload = json.loads(saved.read_text())
    session = Session(saved, payload, digest(saved))
    session.before_submit(1)
    restored, rows = cli._load_plan(saved)
    assert Session(saved, json.loads(saved.read_text()), digest(saved)).uncertain() == [1]
    assert rows == answers


@pytest.mark.parametrize("value", ["", "0", "6", "3.5"])
def test_rating_bounds(value):
    with pytest.raises(ValueError):
        validate_answer(rating(), {1: value})
    assert validate_answer(rating(required=False), {1: ""}) == {1: ""}


def test_unknown_rating_icon_is_not_guessed():
    with pytest.raises(ValueError, match="rating"):
        validate_answer(rating(9), {1: "1"})


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures")
@pytest.mark.parametrize("icon", [1, 2, 3])
def test_rating_fill_readback_change_and_clear(html_page, icon):
    from gformbot.browser import get_driver
    from gformbot.filler import _fill_question, _page_questions

    config = rating(icon=icon, required=False)
    q = config.questions[0]
    choices = "".join(
        # Captured Google behavior: selecting 2 checks icons 1 and 2, not only 2.
        f'<div role="radio" aria-checked="false" data-value="{v}" onclick="document.querySelectorAll(\'[role=radio]\').forEach(e=>e.setAttribute(\'aria-checked\',String(Number(e.dataset.value)<=Number(this.dataset.value))))">★</div>'
        for v in q.options
    )
    url = html_page(
        f'<div role="listitem"><div role="heading">{q.text}</div><input type="hidden" name="entry.{q.entry_id}_sentinel">{choices}<button onclick="document.querySelectorAll(\'[role=radio]\').forEach(e=>e.setAttribute(\'aria-checked\',\'false\'))">Clear selection</button></div>'
    )
    driver = get_driver("chrome", headless=True)
    try:
        driver.get(url)
        question, box = _page_questions(driver, config)[0]
        # Lowering a rating must click even when that icon is already painted checked.
        for value in ("4", "2", "5", "", "1"):
            _fill_question(driver, question, box, value, 2)
            checked = driver.execute_script(
                "return [...document.querySelectorAll('[aria-checked=true]')].map(e=>e.dataset.value)"
            )
            assert checked == [str(i) for i in range(1, int(value or 0) + 1)]
    finally:
        driver.quit()
