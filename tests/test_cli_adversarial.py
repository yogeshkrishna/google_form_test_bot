import copy
import json
from types import SimpleNamespace

import pytest

from gformbot import browser, cli, execution, generator, inspector, logger, transport
from gformbot.config import QuestionConfig, RunConfig


def test_completed_generation_checkpoint_remains_accessible_from_start_menu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checkpoint = tmp_path / "completed-before-export-failed.json"
    checkpoint.write_text(
        json.dumps(
            {
                "kind": "gformbot-generation",
                "responses": [{"1": "Completed profile"}],
                "request": {"count": 1},
            }
        )
    )
    selected, recovered = [], []

    def choose(message, choices, **kwargs):
        if message == "Start":
            return "generation"
        selected.extend(choices)
        assert checkpoint in [item.value for item in choices]
        return checkpoint

    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(cli, "_resume_generation", lambda path, **kw: recovered.append(path))
    cli._wizard()
    assert recovered == [str(checkpoint)]
    assert any("1/1" in item.name and "ready" in item.name for item in selected)


def test_explicitly_saved_plan_survives_confirmed_interactive_execution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=1,
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
    )
    answers = [{1: "Keep this profile"}]
    exported = generator.write_csv(config, answers, tmp_path / "generated.csv")
    plan = cli._save_plan(config, answers, artifacts=[exported], cleanup_on_success=False)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "_next_action", lambda **kw: "submit")
    monkeypatch.setattr(cli, "_choose_browser", lambda: "chrome")
    monkeypatch.setattr(cli, "_confirm_submit", lambda *a: None)

    def confirmed(config, **kwargs):
        config._session.before_submit(1)
        config._record_submission(1)
        return True

    monkeypatch.setattr(cli, "_run_fill", confirmed)
    cli._manage_batch(config, answers, plan, saved=True)
    assert plan.exists() and exported.exists()


def test_instruction_only_form_change_stops_before_browser(tmp_path, monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=1,
        log_file=str(tmp_path / "runs.csv"),
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
        instructions=[{"page": 1, "before_question": 1, "title": "Adults only", "description": ""}],
    )
    changed = copy.deepcopy(config)
    changed.instructions[0]["title"] = "Students only"
    monkeypatch.setattr(inspector, "inspect_form", lambda *a: changed)
    monkeypatch.setattr(execution, "config_from_schema", lambda value: value)
    monkeypatch.setattr(
        browser,
        "get_driver",
        lambda *a, **kw: pytest.fail("Changed instructions must stop before browser launch"),
    )
    with pytest.raises(ValueError, match="instructions have changed"):
        cli._run_fill(config, answers=[{1: "Prepared for adults"}], halt=True)


def test_legacy_plan_without_instruction_metadata_still_loads_and_tests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=1,
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
    )
    answers = [{1: "Legacy profile"}]
    plan = cli._save_plan(config, answers)
    payload = json.loads(plan.read_text())
    payload["config"].pop("instructions")
    plan.write_text(json.dumps(payload))
    restored, rows = cli._load_plan(plan)
    assert restored._instructions_known is False and rows == answers
    current = copy.deepcopy(config)
    current.instructions = [
        {"page": 1, "before_question": 1, "title": "Existing form instructions", "description": ""}
    ]
    opened, closed = [], []
    monkeypatch.setattr(inspector, "inspect_form", lambda *a: current)
    monkeypatch.setattr(execution, "config_from_schema", lambda value: value)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("HTTP check opened a browser"))

    class Client:
        def __init__(self, config):
            assert config._instructions_known is False

        def __enter__(self):
            opened.append(True)
            return self

        def __exit__(self, *args):
            closed.append(True)

        def validate(self, answer, **kwargs):
            return True, "checked"

    monkeypatch.setattr(transport, "Client", Client)
    monkeypatch.setattr(logger, "log_result", lambda *a, **kw: None)
    assert cli._run_fill(restored, answers=rows) is False
    assert opened == closed == [True]
    assert plan.exists()
