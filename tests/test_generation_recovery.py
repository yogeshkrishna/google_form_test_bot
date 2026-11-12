"""The CLI resumes only work that had not reached a durable generation checkpoint."""

import json
import re
from copy import deepcopy

import pytest
from click.testing import CliRunner

from gformbot import ai_partition, chatgpt_http, cli, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig


def _form(count, questions, *, description=""):
    return RunConfig(
        "https://docs.google.com/forms/d/e/recovery/viewform",
        "Test orders",
        form_description=description,
        count=count,
        questions=questions,
    )


def _checkpoint(config):
    (path,) = previews.index_path().with_name("ai-drafts").glob("*.json")
    return Draft.load(path, config)


def _prepare_fixture(config, tmp_path, monkeypatch, *, final_position):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_run_inspect", lambda *args, **kwargs: deepcopy(config))
    monkeypatch.setattr(cli, "_run_submit", lambda *args, **kwargs: pytest.fail("Prepare cannot submit"))
    progress = []
    original = cli._info

    def info(message):
        match = re.fullmatch(r"Creating answers: (\d+)/(\d+) saved", str(message))
        if match:
            done, total = map(int, match.groups())
            assert total == config.count
            checkpoint = _checkpoint(config)
            assert sum(p["position"] == final_position for p in checkpoint.state["profiles"]) == done
            progress.append(done)
        return original(message)

    monkeypatch.setattr(cli, "_info", info)
    return CliRunner(), progress


def _record(identity, answers):
    return json.dumps({"profile_id": identity, "answers": answers})


def test_cli_500_resume_from_491_keeps_exact_answers_and_sends_only_missing_ids(tmp_path, monkeypatch):
    config = _form(500, [QuestionConfig(1, "Order code", "text", True)])
    runner, progress = _prepare_fixture(config, tmp_path, monkeypatch, final_position=1)
    calls = []
    interrupt = True

    def ask(self, messages, *, timeout):
        ids = json.loads(messages[-1]["content"])["profile_numbers"]
        calls.append(ids)
        if interrupt and ids[-1] > 491:
            prefix = "\n".join(_record(i, {"1": f"Order {i}"}) for i in ids if i <= 491)
            raise chatgpt_http.ChatHTTPError(
                "Connection interrupted", partial_content=prefix + '\n{"profile_id":492'
            )
        return "\n".join(_record(i, {"1": f"Order {i}"}) for i in ids)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = runner.invoke(cli.cli, ["prepare", config.url, "-n", "500"])
    assert isinstance(result.exception, ValueError) and "Connection interrupted" in str(result.exception)
    assert "Resume with:" in result.output
    assert progress[-1] == 491
    assert not list(tmp_path.glob("gformbot-responses*.json"))
    saved = _checkpoint(config)
    assert saved.strategy == "flat"
    assert [p["position"] for p in saved.state["profiles"]] == [1] * 491 + [0] * 9
    accepted = [p["answers"] for p in saved.state["profiles"][:491]]

    calls.clear()
    interrupt = False
    resumed = runner.invoke(cli.cli, ["prepare", config.url, "--resume", str(saved.path)])
    assert resumed.exit_code == 0, resumed.exception
    assert [i for batch in calls for i in batch] == list(range(492, 501))
    assert progress[-1] == 500
    (plan,) = tmp_path.glob("gformbot-responses*.json")
    result = json.loads(plan.read_text(encoding="utf-8"))
    assert result["batch_id"] == saved.identity
    assert result["responses"][:491] == accepted
    assert result["responses"][491:] == [{"1": f"Order {i}"} for i in range(492, 501)]


def test_cli_partition_commits_first_reply_before_next_inner_request(tmp_path, monkeypatch):
    config = _form(
        3,
        [QuestionConfig(1, "Order code", "text", True), QuestionConfig(2, "Category", "text", True)],
        description="x" * 18_000,
    )
    runner, progress = _prepare_fixture(config, tmp_path, monkeypatch, final_position=2)
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    calls = []
    interrupt = True

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        (key,) = (question["key"] for question in task["questions"])
        pairs = [(i, task["profile_context"][str(i)]["response_number"]) for i in task["profile_numbers"]]
        calls.extend((key, original) for _, original in pairs)
        if interrupt:
            assert key == "1"
            if pairs[0][1] == 1:
                return _record(pairs[0][0], {"1": "Saved order"})
            saved = _checkpoint(config)
            assert saved.strategy == "partition"
            assert [p["position"] for p in saved.state["profiles"]] == [1, 0, 0]
            assert saved.state["profiles"][0]["answers"] == {"1": "Saved order"}
            raise chatgpt_http.ChatHTTPError("Rate limit", status=429)
        return "\n".join(_record(i, {key: f"Q{key} profile {original}"}) for i, original in pairs)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = runner.invoke(cli.cli, ["prepare", config.url, "-n", "3"])
    assert isinstance(result.exception, ValueError) and "Rate limit" in str(result.exception)
    assert "Resume with:" in result.output
    saved = _checkpoint(config)
    assert not list(tmp_path.glob("gformbot-responses*.json"))

    calls.clear()
    interrupt = False
    resumed = runner.invoke(cli.cli, ["prepare", config.url, "--resume", str(saved.path)])
    assert resumed.exit_code == 0, resumed.exception
    assert sorted(calls) == [("1", 2), ("1", 3), ("2", 1), ("2", 2), ("2", 3)]
    assert progress[-1] == 3
    (plan,) = tmp_path.glob("gformbot-responses*.json")
    result = json.loads(plan.read_text(encoding="utf-8"))
    assert result["batch_id"] == saved.identity
    assert result["responses"][0] == {"1": "Saved order", "2": "Q2 profile 1"}


def test_oversized_legacy_browser_draft_migrates_and_resumes_after_interruption(tmp_path, monkeypatch):
    config = _form(
        2,
        [
            QuestionConfig(1, "Order code", "text", True),
            QuestionConfig(2, "Category", "text", True),
            QuestionConfig(3, "Delivery code", "text", True),
        ],
        description="x" * 18_000,
    )
    runner, _ = _prepare_fixture(config, tmp_path, monkeypatch, final_position=3)
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    accepted = {"1": "Legacy order", "2": "Legacy category", "3": "Legacy delivery code"}
    draft = Draft.create(
        config,
        None,
        "",
        [{}, {2: "Keep this category"}],
        {
            "strategy": "flat",
            "profiles": [
                {"answers": accepted, "position": 1, "route": None},
                {"answers": {}, "position": 0, "route": None},
            ],
            "review": {"checked": [1], "issues": []},
            "setup": {"repair_attempts": {"1": 1}},
        },
        options={"browser": "chrome"},
    )
    calls = []
    interrupted = True

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        (key,) = (q["key"] for q in task["questions"])
        (local,) = task["profile_numbers"]
        assert task["profile_context"][str(local)]["response_number"] == 2
        calls.append(key)
        saved = _checkpoint(config)
        assert saved.strategy == "partition"
        assert saved.identity == draft.identity
        assert saved.state["profiles"][0] == {"answers": accepted, "position": 3, "route": [1]}
        assert saved.state["review"] == draft.state["review"]
        assert saved.state["setup"] == draft.state["setup"]
        if key == "3" and interrupted:
            raise chatgpt_http.ChatHTTPError("Rate limit", status=429)
        values = {"1": "New order", "2": "Keep this category", "3": "New delivery code"}
        return _record(local, {key: values[key]})

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = runner.invoke(cli.cli, ["prepare", config.url, "--resume", str(draft.path)])
    assert isinstance(result.exception, ValueError) and "Rate limit" in str(result.exception)
    assert calls == ["1", "2", "3"]
    saved = _checkpoint(config)
    assert saved.state["profiles"][1] == {
        "answers": {"1": "New order", "2": "Keep this category"},
        "position": 2,
        "route": [1],
    }
    interrupted = False
    calls.clear()
    result = runner.invoke(cli.cli, ["prepare", config.url, "--resume", str(draft.path)])
    assert result.exit_code == 0, result.exception
    assert calls == ["3"]
    (path,) = tmp_path.glob("gformbot-responses*.json")
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["batch_id"] == draft.identity
    assert plan["responses"] == [
        accepted,
        {"1": "New order", "2": "Keep this category", "3": "New delivery code"},
    ]
