import json
import re

import pytest
from test_inspector import URL, document, item

from gformbot import ai, inspector
from gformbot.config import QuestionConfig, RunConfig, config_from_schema, load_config, save_config
from gformbot.generator import load_reference, validate_answer, write_csv


def config(year=True, time=True, required=True):
    return RunConfig(
        URL,
        questions=[
            QuestionConfig(1, "Email", "text", required, entry_id="1", validation=[[2, 102]]),
            QuestionConfig(
                2,
                "Date needed",
                "date",
                required,
                entry_id="2",
                date_include_year=year,
                date_include_time=time,
            ),
        ],
    )


@pytest.mark.parametrize("year,time", [(True, True), (True, False), (False, True), (False, False)])
def test_inspection_date_settings_and_email(year, time, tmp_path):
    date = item(2, "Date needed", 9)
    date[4][0].extend([None, None, [int(time), int(year)]])
    schema = inspector.parse_form_html(document([item(1, "Email", 0, rules=[[2, 102]]), date]), URL)
    schema.require_supported()
    c = config_from_schema(schema)
    assert (c.questions[1].date_include_year, c.questions[1].date_include_time) == (year, time)
    path = tmp_path / "config.yaml"
    save_config(c, path)
    restored = load_config(path)
    assert inspector.schema_fingerprint(restored) == inspector.schema_fingerprint(schema)
    restored.questions[1].date_include_time = not time
    assert inspector.schema_fingerprint(restored) != inspector.schema_fingerprint(schema)


def test_unknown_validation_and_date_settings_remain_explicitly_unsupported():
    schema = inspector.parse_form_html(
        document([item(1, "Pattern", 0, rules=[[2, 999, ["text"]]]), item(2, "Date", 9)]), URL
    )
    assert any("custom validation" in problem for problem in schema.unsupported)
    assert any("date settings" in problem for problem in schema.unsupported)


@pytest.mark.parametrize("email", ["profile@example.com", "test+tag@sub.example.org", "x@y"])
def test_email_syntax(email):
    assert validate_answer(config(), {1: email, 2: "2028-02-29 00:05"})[1] == email


@pytest.mark.parametrize(
    "email", ["not-email", "x@", "x y@example.com", "x@-example.com", "x@example..com", "x@example.com\n"]
)
def test_invalid_email_rejected_before_execution(email):
    with pytest.raises(ValueError, match="email address"):
        validate_answer(config(), {1: email, 2: "2026-10-12 14:30"})


@pytest.mark.parametrize(
    "date",
    [
        "2026-02-29 14:30",
        "2026-13-01 00:00",
        "2026-10-12 24:30",
        "2026-10-12 14:60",
        "12/10/2026 14:30",
        "2026-10-12",
    ],
)
def test_invalid_calendar_or_format_rejected_before_execution(date):
    with pytest.raises(ValueError):
        validate_answer(config(), {1: "profile@example.com", 2: date})


@pytest.mark.parametrize(
    "year,time,date",
    [
        (True, True, "2026-10-12T14:30"),
        (True, False, "2026-10-12"),
        (False, True, "02-29 00:05"),
        (False, False, "02-29"),
    ],
)
def test_supported_date_csv_roundtrip(year, time, date, tmp_path):
    c = config(year, time)
    expected = {1: "profile@example.com", 2: date.replace("T", " ")}
    assert validate_answer(c, {1: expected[1], 2: date}) == expected
    path = tmp_path / "responses.csv"
    write_csv(c, [expected], path)
    assert load_reference(c, path) == [expected]


def test_optional_blanks_preserved_and_required_blanks_rejected():
    assert validate_answer(config(required=False), {1: "", 2: ""}) == {1: "", 2: ""}
    with pytest.raises(ValueError, match="blank"):
        validate_answer(config(), {1: "profile@example.com", 2: ""})


def test_ai_date_and_email_constraints_correct_invalid_answers(monkeypatch):
    contents = iter(
        [
            json.dumps({"profile_id": 1, "answers": {"1": "bad", "2": "2026-02-30 14:30"}}),
            json.dumps({"profile_id": 1, "answers": {"1": "profile@example.com", "2": "2026-10-12 14:30"}}),
        ]
    )
    calls = []

    def chat(driver, messages, schema, timeout, **kwargs):
        calls.append((json.loads(messages[-1]["content"]), schema))
        return next(contents)

    monkeypatch.setattr(ai, "_chat", chat)
    monkeypatch.setattr(ai, "_open_chat", lambda browser: type("Driver", (), {"quit": lambda self: None})())
    assert ai.generate_responses(config(), 1) == [{1: "profile@example.com", 2: "2026-10-12 14:30"}]
    assert len(calls) == 2
    assert "corrections" in calls[1][0]
    fields = calls[0][1]["properties"]
    assert re.fullmatch(fields["1"]["pattern"], "profile@example.com")
    assert not re.fullmatch(fields["1"]["pattern"], "bad")
    assert re.fullmatch(fields["2"]["pattern"], "2026-10-12 14:30")


def test_short_url_resolves_before_parsing(monkeypatch):
    class Response:
        url = URL + "?usp=send_form"
        from email.message import Message

        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return document([item(1, "Name", 0)]).encode()

    requested = []

    def open_url(request, timeout):
        requested.append(request.full_url)
        return Response()

    monkeypatch.setattr(inspector, "urlopen", open_url)
    schema = inspector.inspect_form('"https://forms.gle/RfpthHMQeW81wjFLA"')
    assert requested == ["https://forms.gle/RfpthHMQeW81wjFLA"]
    assert schema.url == URL
    assert len(schema.questions) == 1
