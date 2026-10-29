import json
import os
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai, cli
from gformbot.artifacts import digest
from gformbot.config import config_from_schema
from gformbot.generator import load_reference, prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html
from gformbot.state import Session


def config(checkbox=False):
    raw = json.loads((Path(__file__).parent / "fixtures/google-other.json").read_text(encoding="utf-8"))[
        "items"
    ][int(checkbox)]
    raw[1] = "Tools"
    raw[4][0][1] = [
        ["Python", None, None, None, 0],
        ["Other: Rust", None, None, None, 0],
        ["", None, None, None, 1],
    ]
    raw[4][0][2] = 1
    return config_from_schema(parse_form_html(document([raw]), URL))


@pytest.mark.parametrize("checkbox", [False, True])
def test_other_representation_csv_ai_saved_and_resume(checkbox, tmp_path):
    c = config(checkbox)
    assert not c.unsupported and c.questions[0].allow_other
    answer = {1: {"choices": ["Python"] if checkbox else [], "other": "Rust, C++ | 😀"}}
    assert validate_answer(c, answer) == answer
    path = tmp_path / "other.csv"
    write_csv(c, [answer], path)
    assert load_reference(c, path) == [answer]
    encoded = ai._encode_profile(c, answer)
    assert encoded["1"]["choices"] == ([1] if checkbox else [])
    assert ai._decode_profile(c, encoded) == {"1": answer[1]}
    assert ai._validate_batch(c, json.dumps({"responses": [encoded]}), 1)[0][0] == answer
    saved = cli._save_plan(c, [answer], tmp_path / "other.json", cleanup_on_success=False)
    assert json.loads(saved.read_text())["schema_version"] == 2
    session = Session(saved, json.loads(saved.read_text()), digest(saved))
    session.before_submit(1)
    restored, rows = cli._load_plan(saved)
    assert rows == [answer]
    assert Session(saved, json.loads(saved.read_text()), digest(saved)).uncertain() == [1]
    cli._show_answer(answer, c)


def test_existing_labels_and_formats_preserved():
    assert validate_answer(config(), {1: "Other: Rust"}) == {1: "Other: Rust"}
    assert validate_answer(config(True), {1: ["Python"]}) == {1: ["Python"]}
    assert prepare_responses(config())[0][1] in ("Python", "Other: Rust")
    with pytest.raises(ValueError):
        validate_answer(config(), {1: "Rust"})


def test_other_reference_weights_and_copies():
    from gformbot.generator import apply_reference_weights

    c = config(True)
    profile = {1: {"choices": ["Python"], "other": "Rust"}}
    apply_reference_weights(c, [profile])
    assert c.questions[0].weights == [1, 0]
    context = ai._reference_context(c, [profile])
    assert context["example_profiles"][0]["1"]["other"] == "Rust"
    c.count = 2
    rows = prepare_responses(c, [profile])
    rows[0][1]["choices"].clear()
    assert rows[1] == profile


def test_other_branch_matches_prepared_route():
    from gformbot.routing import Routing

    c = config()
    c.sections = ["First", "Second"]
    c.section_ids = []
    c.questions[0].other_branch = 0
    c.questions[0].branches = {"Python": 2}
    answer = {1: {"choices": [], "other": "Rust"}}
    assert list(Routing(c).walk(answer)) == [1]
    assert Routing(c).choose_route(answer) == answer


@pytest.mark.parametrize(
    "value",
    [
        {"choices": [], "other": ""},
        {"choices": [], "other": "a\nb"},
        {"choices": ["Python"], "other": "Rust"},
        {"choices": [], "other": "Rust", "extra": 1},
    ],
)
def test_invalid_other_radio(value):
    with pytest.raises(ValueError):
        validate_answer(config(), {1: value})


def test_other_counts_and_exclusivity():
    c = config(True)
    q = c.questions[0]
    q.max_select = 1
    with pytest.raises(ValueError, match="at most"):
        validate_answer(c, {1: {"choices": ["Python"], "other": "Rust"}})
    assert validate_answer(c, {1: {"choices": [], "other": "Rust"}})[1]["other"] == "Rust"
    q.max_select = 3
    q.exclusive_options = ["Python"]
    with pytest.raises(ValueError, match="alone"):
        validate_answer(c, {1: {"choices": ["Python"], "other": "Rust"}})


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures")
@pytest.mark.parametrize("checkbox", [False, True])
def test_other_browser_selection_and_text(checkbox, html_page):
    from gformbot.browser import get_driver
    from gformbot.filler import _fill_question, _page_questions

    c = config(checkbox)
    q = c.questions[0]
    role = "checkbox" if checkbox else "radio"
    controls = "".join(
        f'<input type="{role}" name="entry.{q.entry_id}" value="{v}">'
        for v in [*q.options, "__other_option__"]
    )
    url = html_page(
        f'<div role="listitem"><div role="heading">Tools</div>{controls}<input name="entry.{q.entry_id}.other_option_response" type="text"></div>'
    )
    d = get_driver("chrome")
    try:
        d.get(url)
        question, box = _page_questions(d, c)[0]
        answer = {"choices": ["Python"] if checkbox else [], "other": "Rust 😀"}
        _fill_question(d, question, box, answer, 3)
        assert d.execute_script('return [...document.querySelectorAll("input:checked")].map(e=>e.value)') == [
            *answer["choices"],
            "__other_option__",
        ]
        assert d.execute_script('return document.querySelector("input[type=text]").value') == "Rust 😀"
        _fill_question(d, question, box, ["Python"] if checkbox else "Python", 3)
        assert d.execute_script('return document.querySelector("input[type=text]").value') == ""
        assert d.execute_script('return [...document.querySelectorAll("input:checked")].map(e=>e.value)') == [
            "Python"
        ]
    finally:
        d.quit()
