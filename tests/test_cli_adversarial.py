import copy
import json
from types import SimpleNamespace

import pytest

from gformbot import cli, generator, transport
from gformbot.config import QuestionConfig, RunConfig


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
    monkeypatch.setattr(cli, "_next_action", lambda count: "submit")

    def confirmed(config, **kwargs):
        config._session.before_submit(1)
        config._record_submission(1)
        return True

    monkeypatch.setattr(cli, "_run_submit", confirmed)
    cli._manage_batch(config, answers, plan)
    assert plan.exists() and exported.exists()


def test_instruction_only_form_change_stops_before_http_submission(tmp_path, monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=1,
        log_file=str(tmp_path / "runs.csv"),
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
        instructions=[{"page": 1, "before_question": 1, "title": "Adults only", "description": ""}],
    )
    changed = copy.deepcopy(config)
    changed.instructions[0]["title"] = "Students only"
    monkeypatch.setattr(transport, "parse_form_html", lambda *a: changed)
    calls = []

    def request(self, method, url, body=None):
        calls.append(method)
        assert method == "GET", "Changed instructions must stop before POST"
        return 200, {}, ""

    monkeypatch.setattr(transport.Client, "_request", request)
    with pytest.raises(ValueError, match="instructions have changed"):
        cli._run_submit(config, answers=[{1: "Prepared for adults"}])
    assert calls == ["GET"]


def test_legacy_plan_without_instruction_metadata_still_loads_and_submits(tmp_path, monkeypatch):
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
    calls = []
    monkeypatch.setattr(transport, "parse_form_html", lambda *a: current)

    def request(self, method, url, body=None):
        calls.append(method)
        if method == "GET":
            return (
                200,
                {},
                '<input type="hidden" name="fbzx" value="fixture"><input type="hidden" name="pageHistory" value="0">',
            )
        return 200, {}, '<div class="vHW8K">Your response has been recorded.</div>'

    monkeypatch.setattr(transport.Client, "_request", request)
    assert cli._run_submit(restored, answers=rows) is True
    assert calls == ["GET", "POST"]
    assert plan.exists()
