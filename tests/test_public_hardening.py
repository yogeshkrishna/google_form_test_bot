"""Public-form regressions reproduced by the independent repository audit."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from test_inspector import URL, document, item
from test_routing import branch_items

from gformbot import ai, cli
from gformbot.config import (
    QuestionConfig,
    RunConfig,
    config_from_dict,
    config_from_schema,
    config_to_dict,
    load_config,
    save_config,
)
from gformbot.generator import prepare_responses, validate_answer, write_csv
from gformbot.inspector import parse_form_html, schema_fingerprint

CAPTURED = json.loads(
    (Path(__file__).parent / "fixtures/google-public-validation.json").read_text(encoding="utf-8")
)["items"]


@pytest.mark.parametrize("raw", CAPTURED[:2], ids=["safe-endpoint", "absolute-url"])
def test_captured_google_url_rules_survive_inspection_and_validation(raw):
    form = parse_form_html(document([raw]), URL)
    form.require_supported()
    config = config_from_schema(form)
    assert config.questions[0].validation[0][:2] == [2, 103]
    assert validate_answer(config, {1: "https://example.com/test"}) == {1: "https://example.com/test"}


def test_captured_empty_option_is_reported_without_inventing_other():
    form = parse_form_html(document([CAPTURED[2]]), URL)
    assert form.questions[0].options == [""]
    assert form.questions[0].unsupported
    assert not form.questions[0].allow_other


def test_blank_option_is_scoped_to_its_section():
    items = branch_items()
    items[4] = item(3, "Unfinished choice", 2, [""], required=False)
    form = parse_form_html(document(items), URL)
    assert form.questions[2].unsupported == ["empty option labels: Unfinished choice"]
    config = config_from_schema(form)
    config.count = 1
    # The invalid unused section must not prevent a known early-submit route.
    assert prepare_responses(config, [{1: "Skip"}], ordered=True)[0][1] == "Skip"
    assert schema_fingerprint(config_from_dict(config_to_dict(config))) == schema_fingerprint(config)
    with pytest.raises(ValueError, match="empty option"):
        validate_answer(config, {1: "Worker"})


def test_blank_routing_option_still_fails_closed():
    raw = item(1, "Ambiguous route", 2, [""])
    raw[4][0][1][0][2] = -3
    with pytest.raises(ValueError, match="routing"):
        parse_form_html(document([raw]), URL)


@pytest.mark.parametrize(
    "value",
    ["https://example.com", "http://example.com:8080/a?b=1#c", "https://例え.テスト/a", "https://[::1]/"],
)
def test_url_validation_accepts_absolute_urls(value):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Endpoint", "text", True, validation=[[2, 103]])])
    assert validate_answer(config, {1: value}) == {1: value}
    parse_form_html(document([item(1, "Endpoint", 0, rules=[[2, 103]])]), URL).require_supported()


@pytest.mark.parametrize(
    "value",
    [
        "example.com",
        "https://",
        "https://bad host.test",
        "javascript:alert(1)",
        "https://example.com:99999",
        "https://-invalid.test",
        "https://example.com\x00",
    ],
)
def test_url_validation_rejects_invalid_values(value):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Endpoint", "text", True, validation=[[2, 103]])])
    with pytest.raises(ValueError, match="URL"):
        validate_answer(config, {1: value})


def test_config_writers_use_distinct_temporary_files(tmp_path):
    target = tmp_path / "shared.yaml"

    def write(writer):
        for value in range(30):
            save_config(RunConfig(URL, form_title=f"{writer}-{value}"), target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ["A", "B"]))
    assert load_config(target).form_title in {"A-29", "B-29"}
    assert not list(tmp_path.glob("*.tmp"))


def test_config_replace_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    config = RunConfig(URL)
    save_config(config, target)
    before = target.read_bytes()

    def deny_replace(*args):
        raise PermissionError("Replacement denied")

    monkeypatch.setattr(Path, "replace", deny_replace)
    with pytest.raises(PermissionError, match="Replacement denied"):
        save_config(config, target)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "setting", [{"requires_signin": True}, {"one_response": True}, {"email_collection": "verified"}]
)
@pytest.mark.parametrize("complete", [False, True])
def test_ai_rejects_account_restrictions_before_opening_model(setting, complete, monkeypatch):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Name", "text")], **setting)
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Restricted form must not open AI"))
    with pytest.raises(ValueError, match="requires Google sign-in"):
        if complete:
            ai.complete_responses(config, [{1: "Fictional"}])
        else:
            ai.generate_responses(config, 1)


def test_concurrent_csv_export_does_not_overwrite_or_leave_temporary_files(tmp_path):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Name", "text")])
    target = tmp_path / "shared.csv"
    barrier = Barrier(2)

    def write(value):
        barrier.wait()
        try:
            write_csv(config, [{1: value}], target)
            return value
        except ValueError as exc:
            assert "already exists" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["first", "second"]))
    assert len([value for value in results if value]) == 1
    assert target.read_text(encoding="utf-8").splitlines()[1] in results
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_csv_validation_keeps_no_partial_output(tmp_path):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Name", "text", True)])
    target = tmp_path / "invalid.csv"
    with pytest.raises(ValueError, match="required answer"):
        write_csv(config, [{1: ""}], target)
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_public_defaults_preserve_fingerprint_but_upload_plans_stop():
    config = RunConfig(URL, questions=[QuestionConfig(1, "Name", "text")])
    payload = config_to_dict(config)
    payload["questions"][0].update(upload_types=[], upload_max_files=1, upload_max_size=0)
    restored = config_from_dict(payload)
    assert schema_fingerprint(restored) == schema_fingerprint(config)
    assert "upload_types" not in config_to_dict(restored)["questions"][0]
    payload["questions"][0]["type"] = "upload"
    with pytest.raises(ValueError, match="File uploads are not supported"):
        config_from_dict(payload)


def test_retry_corrections_do_not_echo_arbitrary_error_details(monkeypatch):
    secret = r"C:\Users\Someone\Private\customer.pdf"
    config = RunConfig(URL, questions=[QuestionConfig(1, "Name", "text", True)])
    calls = []
    monkeypatch.setattr(ai, "_open_chat", lambda *_: type("Driver", (), {"quit": lambda self: None})())

    def chat(driver, messages, *args, **kwargs):
        calls.append(json.loads(messages[-1]["content"]))
        return '{"responses":[{"1":"Test"}]}'

    monkeypatch.setattr(ai, "_chat", chat)
    real = ai._validate_batch

    def validate(*args, **kwargs):
        return (
            ({}, [f"Profile 1: Q1: Internal diagnostic {secret}"])
            if len(calls) == 1
            else real(*args, **kwargs)
        )

    monkeypatch.setattr(ai, "_validate_batch", validate)
    assert ai.generate_responses(config, 1) == [{1: "Test"}]
    assert len(calls) == 2
    assert "corrections" in calls[1]
    assert secret not in json.dumps(calls)
    assert "customer.pdf" not in json.dumps(calls)


@pytest.mark.parametrize("completed", [False, True])
def test_baseline_generation_job_resumes_without_changing_identity(tmp_path, monkeypatch, completed):
    payload = json.loads(
        (Path(__file__).parent / "fixtures/legacy-public-generation.json").read_text(encoding="utf-8")
    )
    assert "upload_types" in payload["request"]["config"]["questions"][0]
    if not completed:
        payload["responses"][1] = None
    checkpoint = tmp_path / "legacy-generation.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_generation_browser", lambda: "auto")
    monkeypatch.setattr(cli, "_show_preview", lambda *a: None)
    monkeypatch.setattr(cli, "_preview_instructions", lambda *a: None)
    monkeypatch.setattr(ai, "_open_chat", lambda *a: type("Driver", (), {"quit": lambda self: None})())

    def chat(driver, messages, *args, **kwargs):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        assert task["profile_numbers"] == [2]
        return '{"responses":[{"1":2,"2":"Resumed answer"}]}'

    monkeypatch.setattr(ai, "_chat", chat)
    cli._resume_generation(checkpoint, resume=True)
    restored = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert restored["job_key"] == payload["job_key"]
    assert restored["responses"][0] == payload["responses"][0]
    assert all(restored["responses"])
    assert len(calls) == (0 if completed else 1)
