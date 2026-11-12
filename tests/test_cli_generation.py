"""Generation preserves presets and resumes validated large-form drafts."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from gformbot import ai, ai_partition, chatgpt_http, cli, previews
from gformbot.config import QuestionConfig, RunConfig


def _clear_review(task):
    return "\n".join(json.dumps({"response": int(number), "issues": []}) for number in task["profiles"])


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


def test_random_completion_preserves_choices_over_https(generation_config, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a: None)
    monkeypatch.setattr(cli, "_ask_text", lambda *a: "")
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "auto")
    presets = []

    def generate(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        presets.extend(task["partial_profiles"])
        return "\n".join(
            json.dumps({"profile_id": identity, "answers": {**row, "2": "Completed written answer"}})
            for identity, row in zip(task["profile_numbers"], task["partial_profiles"], strict=True)
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", generate)
    answers = cli._prepare_source(generation_config, "random")
    assert len(answers) == 2
    assert [row[1] for row in answers] == [
        generation_config.questions[0].options[p["1"] - 1] for p in presets
    ]
    assert not list(tmp_path.glob("gformbot-generation*"))


def test_failure_does_not_save_or_submit_partial_batch(generation_config, monkeypatch, tmp_path, capsys):
    def fail(*args, **kwargs):
        raise chatgpt_http.ChatHTTPError("Offline")

    monkeypatch.setattr(chatgpt_http.Client, "ask", fail)
    monkeypatch.setattr(
        cli, "_run_submit", lambda *a, **kw: pytest.fail("Incomplete AI output cannot submit")
    )
    with pytest.raises(ai.GenerationError, match="Offline"):
        cli._generate(generation_config)
    assert not list(tmp_path.glob("*.json"))
    output = capsys.readouterr().out
    assert "--resume" in output and "--data" in output


def test_oversized_https_prompt_stops_before_sending(generation_config, monkeypatch):
    # JSON escaping makes this fit the general context budget but not the wire budget.
    generation_config.questions[0].description = '"' * 35_000
    monkeypatch.setattr(chatgpt_http.Client, "ask", lambda *a, **kw: pytest.fail("Oversized prompt was sent"))
    with pytest.raises(ai.GenerationError, match="exceeds the ChatGPT HTTPS prompt budget"):
        cli._generate(generation_config)


def test_transient_failure_keeps_valid_rows_and_retries_only_missing_ids(generation_config, monkeypatch):
    calls = []

    def generate(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        ids = task["profile_numbers"]
        calls.append(ids)
        if len(calls) == 1:
            error = chatgpt_http.ChatHTTPError(
                "Connection interrupted",
                partial_content=json.dumps({"profile_id": 1, "answers": {"1": 1, "2": "Keep me"}}),
            )
            error.retryable = True
            raise error
        return json.dumps({"profile_id": 2, "answers": {"1": 2, "2": "Recovered"}})

    monkeypatch.setattr(chatgpt_http.Client, "ask", generate)
    monkeypatch.setattr(ai.time, "sleep", lambda _: None)
    assert cli._generate(generation_config) == [{1: "A", 2: "Keep me"}, {1: "B", 2: "Recovered"}]
    assert calls == [[1, 2], [2]]


def test_transient_retries_are_bounded(generation_config, monkeypatch):
    calls = []

    def fail(self, messages, *, timeout):
        calls.append(True)
        error = chatgpt_http.ChatHTTPError("Connection interrupted")
        error.retryable = True
        raise error

    monkeypatch.setattr(chatgpt_http.Client, "ask", fail)
    monkeypatch.setattr(ai.time, "sleep", lambda _: None)
    with pytest.raises(ai.GenerationError, match="after 3 attempts"):
        cli._generate(generation_config)
    assert len(calls) == 3


def test_length_failure_sends_precise_rules_without_echoing_rejected_answer(generation_config, monkeypatch):
    generation_config.count = 1
    generation_config.questions[1].validation = [[6, 202, ["90"]]]
    calls = []

    def generate(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        calls.append(task)
        if len(calls) == 1:
            reason = "private rejected text " * 10
        else:
            correction = " ".join(task["corrections"])
            assert "At most 90 characters" in correction
            assert "60 UTF-16 units" in correction
            assert "private rejected text" not in correction
            reason = "Concise valid reason"
        return json.dumps({"profile_id": 1, "answers": {"1": 1, "2": reason}})

    monkeypatch.setattr(chatgpt_http.Client, "ask", generate)
    assert cli._generate(generation_config) == [{1: "A", 2: "Concise valid reason"}]
    assert len(calls) == 2


def test_completion_rejects_conflicting_presets_before_ai_request(generation_config, monkeypatch):
    generation_config.questions[0].fixed_value = "B"
    monkeypatch.setattr(
        chatgpt_http.Client, "ask", lambda *a, **k: pytest.fail("Conflicting presets reached AI")
    )
    with pytest.raises(ValueError, match="fixed|preselected|conflict"):
        ai.complete_responses(generation_config, [{1: "A"}])


def test_grid_retry_names_exact_keys_and_keeps_unaffected_answers(generation_config, monkeypatch):
    generation_config.questions.extend(
        QuestionConfig(
            index,
            f"S08-Q09 [{index}]",
            "radio",
            True,
            ["Red", "Blue"],
            grid_title="S08-Q09 — Matrix",
            grid_row=str(index),
            grid_unique=True,
            item_id="matrix",
            entry_id=str(index),
        )
        for index in (3, 4)
    )
    calls = []

    def generate(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        calls.append(task)
        if len(calls) == 1:
            assert task["unique_grid_rows"] == [["3", "4"]]
            return "\n".join(
                json.dumps(
                    {"profile_id": number, "answers": {"1": 1, "2": f"Keep {number}", "3": 1, "4": number}}
                )
                for number in (1, 2)
            )
        assert task["profile_numbers"] == [1]
        assert task["repair_fields"] == {"1": ["3", "4"]}
        assert {q["key"] for q in task["questions"]} == {"3", "4"}
        correction = " ".join(task["corrections"])
        assert "keys 3 and 4 both selected column ID 1" in correction
        return json.dumps({"profile_id": 1, "answers": {"3": 2, "4": 1}})

    monkeypatch.setattr(chatgpt_http.Client, "ask", generate)
    assert cli._generate(generation_config) == [
        {1: "A", 2: "Keep 1", 3: "Blue", 4: "Red"},
        {1: "A", 2: "Keep 2", 3: "Red", 4: "Blue"},
    ]
    assert len(calls) == 2


def test_prepare_resume_preserves_validated_group_and_plan_identity(generation_config, monkeypatch, tmp_path):
    generation_config.form_description = "x" * 18_000
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: deepcopy(generation_config))
    monkeypatch.setattr(cli, "_run_submit", lambda *a, **kw: pytest.fail("Prepare cannot submit"))
    calls = []
    interrupted = True

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        keys = tuple(q["key"] for q in task["questions"])
        calls.append(keys)
        if keys == ("2",) and interrupted:
            raise chatgpt_http.ChatHTTPError("Rate limit", status=429)
        values = {"1": 2, "2": "Completed later"}
        return "\n".join(
            json.dumps({"profile_id": i, "answers": {key: values[key] for key in keys}})
            for i in task["profile_numbers"]
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["prepare", generation_config.url, "-n", "2"])
    assert result.exit_code != 0 and "Resume with:" in result.output
    assert calls == [("1",), ("2",)]
    assert not list(tmp_path.glob("gformbot-responses*.json"))
    drafts = [
        path
        for path in (tmp_path / "state").rglob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("kind") != "gformbot-run"
    ]
    (draft,) = drafts
    from gformbot.ai_draft import Draft

    identity = Draft.load(draft, generation_config).identity
    interrupted = False
    result = runner.invoke(cli.cli, ["prepare", generation_config.url, "--resume", str(draft)])
    assert result.exit_code == 0, result.exception
    assert calls == [("1",), ("2",), ("2",)]
    (plan,) = tmp_path.glob("gformbot-responses*.json")
    assert json.loads(plan.read_text(encoding="utf-8"))["batch_id"] == identity
    _, rows = cli._load_plan(plan)
    assert rows == [{1: "B", 2: "Completed later"}] * 2
