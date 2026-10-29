"""Generation commands exercise real checkpoints without live model traffic."""

import json

import pytest

from gformbot import ai, cli, generator
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def generation_config():
    return RunConfig(
        "https://docs.google.com/forms/d/e/fixture/viewform",
        "Fixture",
        count=2,
        questions=[
            QuestionConfig(1, "Choice", "radio", True, ["A", "B"]),
            QuestionConfig(2, "Reason", "text", True),
        ],
    )


class Driver:
    def quit(self):
        pass


def incomplete_job(config, path, monkeypatch):
    monkeypatch.setattr(ai, "_open_chat", lambda *a: Driver())
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps({"responses": [{"1": 1, "2": "Saved first profile"}]})
        raise ai.ChatGPTError("Limit reached")

    monkeypatch.setattr(ai, "_chat", generate)
    with pytest.raises(ai.GenerationError):
        ai.generate_responses(config, 2, batch_size=1, checkpoint=path)


def test_random_completion_uses_selected_browser_and_durable_checkpoint(
    generation_config, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a: None)
    monkeypatch.setattr(cli, "_ask_text", lambda *a: "")
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "auto")
    monkeypatch.setattr(cli, "_generation_browser", lambda: "firefox")
    browsers = []
    monkeypatch.setattr(ai, "_open_chat", lambda browser: browsers.append(browser) or Driver())

    def generate(driver, messages, *a, **kw):
        task = json.loads(messages[-1]["content"])
        return json.dumps(
            {"responses": [{**row, "2": "Completed written answer"} for row in task["partial_profiles"]]}
        )

    monkeypatch.setattr(ai, "_chat", generate)
    answers = cli._prepare_source(generation_config, "random")
    assert len(answers) == 2 and browsers == ["firefox"]
    path = generation_config._temporary_checkpoint
    saved = json.loads(path.read_text())
    assert saved["kind"] == "gformbot-generation"
    assert saved["request"]["partials"] and all(saved["responses"])


def test_generation_resume_command_keeps_saved_rows_and_generates_only_missing(
    generation_config, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "job.json"
    incomplete_job(generation_config, path, monkeypatch)
    monkeypatch.setattr(cli, "_generation_browser", lambda: "firefox")
    calls = []

    def generate(driver, messages, *a, **kw):
        calls.append(json.loads(messages[-1]["content"]))
        return json.dumps({"responses": [{"1": 2, "2": "New second profile"}]})

    monkeypatch.setattr(ai, "_chat", generate)
    cli._resume_generation(str(path), resume=True, export=None)
    assert len(calls) == 1 and calls[0]["profile_numbers"] == [2]
    plans = list(tmp_path.glob("gformbot-responses*.json"))
    assert len(plans) == 1
    restored, answers = cli._load_plan(plans[0])
    assert restored.count == 2
    assert [row[2] for row in answers] == ["Saved first profile", "New second profile"]


def test_generation_export_valid_subset_never_opens_browser(generation_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "job.json"
    incomplete_job(generation_config, path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Export must stay offline"))
    output = tmp_path / "partial.csv"
    cli._resume_generation(str(path), resume=False, export=str(output))
    assert generator.load_reference(generation_config, output) == [{1: "A", 2: "Saved first profile"}]
    assert path.read_bytes() == before


def test_generation_resume_and_export_are_mutually_exclusive(generation_config, monkeypatch, tmp_path):
    path = tmp_path / "job.json"
    incomplete_job(generation_config, path, monkeypatch)
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Invalid options must not launch"))
    with pytest.raises(ValueError, match="either"):
        cli._resume_generation(str(path), resume=True, export=str(tmp_path / "partial.csv"))


def test_completed_generation_resume_does_not_require_browser_selection(
    generation_config, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "job.json"
    incomplete_job(generation_config, path, monkeypatch)
    monkeypatch.setattr(ai, "_chat", lambda *a, **kw: json.dumps({"responses": [{"1": 2, "2": "Completed"}]}))
    ai.generate_responses(generation_config, 2, checkpoint=path)
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Completed job stays offline"))
    monkeypatch.setattr(
        cli, "_generation_browser", lambda: pytest.fail("Completed job needs no browser choice")
    )
    cli._resume_generation(str(path), resume=True, export=None)
    assert len(list(tmp_path.glob("gformbot-responses*.json"))) == 1


def test_generation_export_requires_importable_csv_suffix(generation_config, monkeypatch, tmp_path):
    path = tmp_path / "job.json"
    incomplete_job(generation_config, path, monkeypatch)
    output = tmp_path / "partial.txt"
    with pytest.raises(ValueError, match=".csv"):
        cli._resume_generation(str(path), resume=False, export=str(output))
    assert not output.exists()


def test_completion_rejects_fixed_value_conflict_before_opening_browser(
    generation_config, monkeypatch, tmp_path
):
    generation_config.questions[0].fixed_value = "B"
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Conflicting presets must fail first"))
    with pytest.raises(ValueError, match="fixed|preselected|conflict"):
        ai.complete_responses(generation_config, [{1: "A"}], checkpoint=tmp_path / "conflict.json")


def test_checkpoint_write_failure_preserves_durable_file_and_closes_browser(
    generation_config, monkeypatch, tmp_path
):
    path = tmp_path / "job.json"
    closed = []

    class FailingSaveDriver:
        def quit(self):
            closed.append(True)

    monkeypatch.setattr(ai, "_open_chat", lambda *a: FailingSaveDriver())
    monkeypatch.setattr(
        ai, "_chat", lambda *a, **kw: json.dumps({"responses": [{"1": 1, "2": "Valid in memory"}]})
    )
    save = ai._save_checkpoint
    writes = []

    def save_once_then_fail(target, payload):
        writes.append(1)
        if len(writes) > 1:
            raise OSError("disk full")
        save(target, payload)

    monkeypatch.setattr(ai, "_save_checkpoint", save_once_then_fail)
    with pytest.raises(ai.GenerationError, match="0/1 responses saved") as failure:
        ai.generate_responses(generation_config, 1, checkpoint=path)
    assert failure.value.partial_responses == [{1: "A", 2: "Valid in memory"}]
    assert json.loads(path.read_text())["responses"] == [None]
    assert closed == [True]
    with ai._checkpoint_lock(path):
        pass
