"""Exercise form context, deterministic quality, repair, and review recovery through the CLI."""

import json
from copy import deepcopy

from click.testing import CliRunner

from gformbot import chatgpt_http, cli, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation import Generation


def _setup(monkeypatch, tmp_path, questions, *, count=1):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
    config = RunConfig(
        "https://docs.google.com/forms/d/e/quality/viewform",
        "Test report",
        count=count,
        form_description="Summarize the current equipment state for the next shift.",
        questions=questions,
    )
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **k: deepcopy(config))
    return config


def _policy(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def _plan(path):
    return json.loads(next(path.glob("gformbot-responses*.json")).read_text(encoding="utf-8"))


def test_review_context_names_reached_route_sections():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/quality/viewform",
        sections=["Gate", "Chosen branch", "Skipped branch", "Readback"],
        section_next=[2, 4, 4, 0],
        questions=[QuestionConfig(1, "Was the previous branch completed?", "text", page=4)],
    )
    context = Generation(config, None, "")._review_context({1: {"route": [1, 2, 4], "answers": {1: "Yes"}}})
    assert context["route_sections"] == {1: "Gate", 2: "Chosen branch", 4: "Readback"}
    assert context["questions"][1]["page"] == 4


def test_review_repairs_only_contradictory_field_and_checks_again(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Asset category", "radio", True, ["Gateway", "Sensor"]),
            QuestionConfig(2, "Lifecycle state", "radio", True, ["Operational", "Retired"]),
            QuestionConfig(
                3, "Write a handoff summary", "textarea", True, description="Match the asset state."
            ),
        ],
    )
    policy = _policy(tmp_path / "rules.json", {"facts": [1, 2], "dependencies": {"3": [1, 2]}})
    calls = []

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        if "repair" in task:
            calls.append("repair")
            assert [q["index"] for q in task["repair_questions"]] == [3]
            assert '"profile_id": 1' in messages[0]["content"]
            assert 'only keys inside answers must be "3"' in messages[0]["content"]
            return '{"profile_id":1,"answers":{"3":"Gateway is operational. The next shift can leave it running."}}'
        if "profiles" in task:
            calls.append("review")
            profile = task["profiles"]["1"]
            assert profile["facts"] == {"1": "Gateway", "2": "Operational"}
            if profile["answers"]["3"].startswith("Retired sensor"):
                return json.dumps(
                    {
                        "response": 1,
                        "issues": [
                            {
                                "question": 3,
                                "kind": "contradiction",
                                "reason": "The gateway is operational.",
                                "evidence": [{"question": 3, "quote": "Retired sensor"}],
                            }
                        ],
                    }
                )
            return '{"response":1,"issues":[]}'
        calls.append("generate")
        assert task["form_description"] == config.form_description
        assert task["questions"][0]["options"] == {"1": "Gateway", "2": "Sensor"}
        assert task["questions"][2]["description"] == "Match the asset state."
        assert task["generation_brief"]["answer_dependencies"] == {"3": ["1", "2"]}
        return '{"profile_id":1,"answers":{"1":1,"2":1,"3":"Retired sensor needs pickup."}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "--quality", policy])
    assert result.exit_code == 0, result.output
    assert calls == ["generate", "review", "repair", "review"]
    assert _plan(tmp_path)["responses"] == [
        {
            "1": "Gateway",
            "2": "Operational",
            "3": "Gateway is operational. The next shift can leave it running.",
        }
    ]


def test_short_form_comments_are_reviewed_against_selected_choices_and_quantity(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Cake", "radio", True, ["Carrot", "Chocolate"]),
            QuestionConfig(2, "Number of cakes", "text", True),
            QuestionConfig(3, "Comments", "textarea"),
        ],
    )
    calls = []

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        if "repair" in task:
            calls.append("repair")
            assert task["profile"]["answers"]["1"] == "Carrot"
            assert task["profile"]["answers"]["2"] == "6"
            assert [q["index"] for q in task["repair_questions"]] == [3]
            return '{"profile_id":1,"answers":{"3":"Please box the six carrot cupcakes together."}}'
        if "profiles" in task:
            calls.append("review")
            profile = task["profiles"]["1"]
            assert profile["review_keys"] == [3]
            assert profile["answers"]["1"] == "Carrot"
            assert profile["answers"]["2"] == "6"
            if "chocolate" in profile["answers"]["3"]:
                return json.dumps(
                    {
                        "response": 1,
                        "issues": [
                            {
                                "question": 3,
                                "kind": "contradiction",
                                "reason": "Q1 selects carrot and Q2 specifies six.",
                                "evidence": [{"question": 3, "quote": "four chocolate"}],
                            }
                        ],
                    }
                )
            return '{"response":1,"issues":[]}'
        calls.append("generate")
        return '{"profile_id":1,"answers":{"1":1,"2":"6","3":"Please box the four chocolate cupcakes together."}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url])
    assert result.exit_code == 0, result.exception
    assert calls == ["generate", "review", "repair", "review"]
    assert _plan(tmp_path)["responses"] == [
        {"1": "Carrot", "2": "6", "3": "Please box the six carrot cupcakes together."}
    ]


def test_resume_draft_with_retired_reviewer_keeps_answers_and_review_progress(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Asset category", "radio", True, ["Gateway", "Sensor"]),
            QuestionConfig(2, "Handoff note", "textarea", True),
        ],
        count=2,
    )
    rows = [
        {"1": "Gateway", "2": "The gateway is ready for the next shift."},
        {"1": "Sensor", "2": "The sensor is online and reporting."},
    ]
    prior_issue = {"response": 1, "question": 2, "reason": "Review saved a warning.", "blocking": False}
    draft = Draft.create(
        config,
        None,
        "",
        None,
        {
            "strategy": "flat",
            "profiles": [{"answers": row, "position": 1, "route": None} for row in rows],
            "review": {"checked": [1], "issues": [prior_issue]},
            "setup": {},
        },
        options={"browser": None, "policy": {"dependencies": {"2": [1]}}, "review_backend": "distilabel"},
    )
    calls = []

    def ask(self, messages, **kwargs):
        profiles = json.loads(messages[-1]["content"])["profiles"]
        calls.append(profiles)
        assert set(profiles) == {"2"}
        assert profiles["2"]["answers"] == rows[1]
        return '{"response":2,"issues":[]}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "--resume", str(draft.path)])
    assert result.exit_code == 0, result.output or str(result.exception)
    assert len(calls) == 1
    plan = _plan(tmp_path)
    assert plan["batch_id"] == draft.identity
    assert plan["responses"] == rows
    assert plan["quality_warnings"] == [prior_issue]


def test_malformed_line_does_not_lose_later_row_and_phone_rule_is_enforced(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Full name", "text", True),
            QuestionConfig(2, "Email", "text", True),
            QuestionConfig(3, "Phone number", "text", True),
        ],
        count=3,
    )
    policy = _policy(
        tmp_path / "rules.json",
        {
            "fields": {"2": {"email_domain": "test.example", "name_from": 1}, "3": {"min_digits": 10}},
        },
    )
    calls = []

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        calls.append(task["profile_numbers"])
        if len(calls) == 1:
            return (
                '{"profile_id":1,"answers":{"1":"Maya Shah","2":"placeholder@test.example","3":"303 555 0191"}}\n'
                '{"profile_id":2,"answers":{"broken"\n'
                '{"profile_id":3,"answers":{"1":"Ravi Mehta","2":"placeholder@test.example","3":"303 555 0193"}}'
            )
        if len(calls) == 2:
            return '{"profile_id":2,"answers":{"1":"Leah Rao","2":"placeholder@test.example","3":"555"}}'
        assert task["repair_fields"] == {"2": ["3"]}
        return '{"profile_id":2,"answers":{"3":"303 555 0192"}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "-n", "3", "--quality", policy])
    assert result.exit_code == 0, result.output
    assert calls == [[1, 2, 3], [2], [2]]
    assert [row["2"] for row in _plan(tmp_path)["responses"]] == [
        "maya.shah@test.example",
        "leah.rao@test.example",
        "ravi.mehta@test.example",
    ]


def test_required_media_can_be_resolved_without_regenerating_saved_rows(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "What is shown in the image?", "radio", True, ["Circle", "Square"]),
        ],
    )
    calls = []

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        if "profiles" in task:
            assert task["form"]["brief"]["supplied_evidence"] == {"1": "The image shows a square."}
            if task["profiles"]["1"]["answers"]["1"] == "Circle":
                return json.dumps(
                    {
                        "response": 1,
                        "issues": [
                            {
                                "question": 1,
                                "kind": "contradiction",
                                "reason": "Supplied evidence specifies a square.",
                                "evidence": [{"question": 1, "quote": "Circle"}],
                            }
                        ],
                    }
                )
            return '{"response":1,"issues":[]}'
        if "repair" in task:
            return '{"profile_id":1,"answers":{"1":2}}'
        return '{"profile_id":1,"answers":{"1":1}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["prepare", config.url])
    assert result.exit_code == 1
    assert "Answer depends on media" in str(result.exception)
    (path,) = (tmp_path / "state" / "ai-drafts").glob("*.json")
    original = Draft.load(path, config)
    assert original.state["profiles"][0]["answers"] == {"1": "Circle"}
    policy = _policy(tmp_path / "evidence.json", {"evidence": {"1": "The image shows a square."}})
    result = runner.invoke(cli.cli, ["prepare", config.url, "--resume", str(path), "--quality", policy])
    assert result.exit_code == 0, result.output
    assert len(calls) == 4
    assert _plan(tmp_path)["batch_id"] == original.identity
    assert _plan(tmp_path)["responses"] == [{"1": "Square"}]
