"""Generation keeps current-run presets without creating saved AI jobs."""

import json

import pytest

from gformbot import ai, cli
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def generation_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return RunConfig(
        "https://docs.google.com/forms/d/e/fixture/viewform",
        "Fixture",
        count=2,
        questions=[
            QuestionConfig(1, "Choice", "radio", True, ["A", "B"]),
            QuestionConfig(2, "Reason", "text", True),
        ],
    )


def test_random_completion_preserves_choices_and_uses_selected_browser(
    generation_config, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a: None)
    monkeypatch.setattr(cli, "_ask_text", lambda *a: "")
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "auto")
    generation_config.browser = "firefox"
    browsers, presets = [], []
    driver = type("Driver", (), {"quit": lambda self: None})()
    monkeypatch.setattr(ai, "_open_chat", lambda browser: browsers.append(browser) or driver)

    def generate(driver, messages, *a, **kw):
        task = json.loads(messages[-1]["content"])
        presets.extend(task["partial_profiles"])
        return "\n".join(
            json.dumps({"profile_id": identity, "answers": {**row, "2": "Completed written answer"}})
            for identity, row in zip(task["profile_numbers"], task["partial_profiles"], strict=True)
        )

    monkeypatch.setattr(ai, "_chat", generate)
    answers = cli._prepare_source(generation_config, "random")
    assert len(answers) == 2 and browsers == ["firefox"]
    assert [row[1] for row in answers] == [
        generation_config.questions[0].options[p["1"] - 1] for p in presets
    ]
    assert not list(tmp_path.glob("gformbot-generation*"))


def test_failure_does_not_save_or_submit_partial_batch(generation_config, monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_open_chat", lambda *a: type("Driver", (), {"quit": lambda self: None})())
    monkeypatch.setattr(ai, "_chat", lambda *a, **kw: (_ for _ in ()).throw(ai.ChatGPTError("Offline")))
    monkeypatch.setattr(
        cli, "_run_submit", lambda *a, **kw: pytest.fail("Incomplete AI output cannot submit")
    )
    with pytest.raises(ai.GenerationError, match="Offline"):
        cli._generate(generation_config)
    assert not list(tmp_path.glob("*.json"))


def test_completion_rejects_conflicting_presets_before_browser(generation_config, monkeypatch):
    generation_config.questions[0].fixed_value = "B"
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Conflicting presets must fail first"))
    with pytest.raises(ValueError, match="fixed|preselected|conflict"):
        ai.complete_responses(generation_config, [{1: "A"}])
