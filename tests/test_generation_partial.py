"""Check focused repairs and generation checkpoints without submitting responses."""

import json

import pytest

from gformbot import ai, chatgpt_http, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation import Generation


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "previews.json")
    return RunConfig(
        "https://docs.google.com/forms/d/e/quality/viewform",
        "Test report",
        questions=[QuestionConfig(1, "Power", "radio", True, ["On", "Off"])],
        count=3,
    )


@pytest.mark.parametrize("invalid", [{2}, {1, 2, 3}])
def test_incomplete_generation_checkpoints_then_resumes_only_failed_profiles(config, monkeypatch, invalid):
    calls = []
    failing = invalid.copy()

    def ask(self, messages, *, timeout):
        ids = json.loads(messages[-1]["content"])["profile_numbers"]
        calls.append(ids)
        return "\n".join(
            json.dumps({"profile_id": n, "answers": {"1": 9 if n in failing else 2 if n == 3 else 1}})
            for n in ids
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    job = Generation(config, None, "")
    with pytest.raises(ai.GenerationError, match=rf"Prepared {3 - len(invalid)}/3 responses"):
        job.run()
    assert calls == [[1, 2, 3], sorted(invalid), sorted(invalid)]
    assert config.count == 3
    draft = Draft.read(job.draft.path)
    assert [p["position"] for p in draft.state["profiles"]] == [int(n not in invalid) for n in (1, 2, 3)]

    calls.clear()
    failing.clear()
    assert Generation(draft.config, None, "", draft=draft).run() == [{1: "On"}, {1: "On"}, {1: "Off"}]
    assert calls == [sorted(invalid)]


def test_legacy_draft_cannot_return_partial_batch(config, monkeypatch):
    state = {
        "strategy": "flat",
        "profiles": [
            {"answers": {"1": "On"}, "position": 1, "route": None},
            {"answers": {}, "position": 0, "route": None},
            {"answers": {"1": "Off"}, "position": 1, "route": None},
        ],
        "review": {"checked": [], "issues": []},
        "setup": {},
    }
    draft = Draft.create(config, None, "", None, state, options={"allow_partial": True})

    def ask(self, messages, *, timeout):
        assert json.loads(messages[-1]["content"])["profile_numbers"] == [2]
        return '{"profile_id":2,"answers":{"1":9}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    with pytest.raises(ai.GenerationError, match="Prepared 2/3 responses"):
        Generation(draft.config, None, "", draft=draft).run()

    reopened = Draft.read(draft.path)
    assert reopened.identity == draft.identity
    assert reopened.state["profiles"] == state["profiles"]


def test_incomplete_record_repairs_only_missing_keys(config, monkeypatch):
    config.questions.extend(
        [
            QuestionConfig(2, "Ready", "radio", True, ["Yes", "No"]),
            QuestionConfig(3, "Verified", "radio", True, ["Yes", "No"]),
        ]
    )
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        if len(calls) == 1:
            return "\n".join(
                json.dumps({"profile_id": number, "answers": answers})
                for number, answers in (
                    (1, {"1": 2, "2": 2, "3": 2}),
                    (2, {"1": 1}),
                    (3, {"1": 2, "2": 1, "3": 1}),
                )
            )
        assert task["profile_numbers"] == [2]
        assert task["repair_fields"] == {"2": ["2", "3"]}
        # A full echo must not overwrite a validated field.
        return '{"profile_id":2,"answers":{"1":2,"2":1,"3":2}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert ai.generate_responses(config, 3) == [
        {1: "Off", 2: "No", 3: "No"},
        {1: "On", 2: "Yes", 3: "No"},
        {1: "Off", 2: "Yes", 3: "Yes"},
    ]
    assert len(calls) == 2


@pytest.mark.parametrize("missing", ["1", "2"])
def test_branch_record_patch_requires_known_route(config, monkeypatch, missing):
    config.sections = ["Start", "Follow-up", "Other route"]
    config.questions[0].branches = {"On": 2, "Off": 3}
    config.questions.extend(
        [
            QuestionConfig(2, "Ready", "radio", True, ["Yes", "No"], page=2),
            QuestionConfig(3, "Verified", "radio", True, ["Yes", "No"], page=3),
        ]
    )
    config.section_next = [2, 0, 0]
    complete = {"1": 1, "2": 2, "3": 0}
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        if len(calls) == 1:
            row = {key: value for key, value in complete.items() if key != missing}
        elif missing == "2":
            assert task["repair_fields"] == {"1": ["2"]}
            row = {"2": 2}
        else:
            assert "repair_fields" not in task
            row = complete
        return json.dumps({"profile_id": 1, "answers": row})

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert ai.generate_responses(config, 1) == [{1: "On", 2: "No", 3: ""}]
    assert len(calls) == 2


def test_large_guided_batch_repairs_one_incomplete_profile_without_replaying_others(config, monkeypatch):
    config.count = 500
    config.sections = ["Start", "Follow-up", "Other route"]
    config.section_next = [2, 0, 0]
    config.questions[0].branches = {"On": 2, "Off": 3}
    config.questions.extend(
        QuestionConfig(
            key, f"Calibration item {key}", "radio", True, ["Yes", "No"], page=2 if key < 45 else 3
        )
        for key in range(2, 87)
    )
    missing_key = "3"
    original_requests = []
    repairs = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if fields := task.get("repair_fields"):
            repairs.append(fields)
            assert len(fields) == 1
            [(identity, keys)] = fields.items()
            assert keys == [missing_key]
            return json.dumps({"profile_id": int(identity), "answers": {missing_key: 2}})
        questions = task["questions"]
        if isinstance(questions, dict):
            column = questions["columns"].index("key")
            keys = [row[column] for row in questions["rows"]]
        else:
            keys = [question["key"] for question in questions]
        result = []
        for identity in task["profile_numbers"]:
            context = task.get("profile_context", {}).get(str(identity), {})
            number = context.get("response_number", identity)
            original_requests.extend((number, key) for key in keys)
            answers = {key: int(int(key) < 45) for key in keys}
            if number == 59:
                answers.pop(missing_key, None)
            result.append(json.dumps({"profile_id": identity, "answers": answers}))
        return "\n".join(result)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    reference = {1: "On", **{key: "Yes" if key < 45 else "" for key in range(2, 87)}}
    rows = ai.generate_responses(
        config, config.count, reference=[reference], instructions="Choose concise, plausible answers."
    )
    assert len(repairs) == 1
    assert len(rows) == 500 and rows[58] == {**reference, 3: "No"}
    assert all(row == reference for number, row in enumerate(rows, 1) if number != 59)
    assert len(original_requests) == len(set(original_requests))


def test_last_retry_isolates_records_that_were_missing_from_grouped_replies(config, monkeypatch):
    config.count = 16
    calls = []

    def ask(self, messages, *, timeout):
        ids = json.loads(messages[-1]["content"])["profile_numbers"]
        calls.append(ids)
        if len(calls) == 1:
            ids = ids[:1]
        elif len(ids) > 1:
            return "The response was truncated."
        return "\n".join(json.dumps({"profile_id": n, "answers": {"1": 1}}) for n in ids)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert ai.generate_responses(config, config.count) == [{1: "On"}] * config.count
    assert calls[:3] == [list(range(1, 17)), list(range(2, 10)), [2]]
    assert all(sum(n in group for group in calls) <= 3 for n in range(1, 17))


def test_generation_preserves_response_ids_when_review_repair_fails(config, monkeypatch):
    config.questions.append(QuestionConfig(2, "Write a handoff summary", "textarea", True))
    notes = {
        1: "The first response records the handoff.",
        2: "The second response records a separate handoff.",
        3: "The last report still needs correction.",
    }
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "repair" in task:
            calls.append(("repair", task["profile_numbers"]))
            return json.dumps({"profile_id": 3, "answers": {"2": notes[3]}})
        if "profiles" in task:
            ids = list(map(int, task["profiles"]))
            calls.append(("review", ids))
            return "\n".join(
                json.dumps(
                    {
                        "response": n,
                        "issues": [
                            {
                                "question": 2,
                                "kind": "contradiction",
                                "reason": "Use the handoff notes.",
                                "evidence": [{"question": 2, "quote": notes[n]}],
                            }
                        ]
                        if n == 3
                        else [],
                    }
                )
                for n in ids
            )
        ids = task["profile_numbers"]
        calls.append(("generate", ids))
        return "\n".join(json.dumps({"profile_id": n, "answers": {"1": 1, "2": notes[n]}}) for n in ids)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    job = Generation(config, None, "", policy={"dependencies": {"2": [1]}})

    with pytest.raises(ValueError, match="Response 3, Q2 needs review"):
        job.run()
    assert calls == [
        ("generate", [1, 2, 3]),
        ("review", [1, 2, 3]),
        ("repair", [3]),
        ("repair", [3]),
    ]
    saved = Draft.read(job.draft.path).state
    assert saved["review"]["checked"] == [1, 2, 3]
    assert saved["review"]["issues"][0]["response"] == 3


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, chatgpt_http.ChatHTTPError])
def test_generation_checkpoints_and_raises_interrupted_review(config, monkeypatch, exc_type):
    config.questions.append(QuestionConfig(2, "Write a handoff summary", "textarea", True))

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            exc = exc_type("Test interruption")
            exc.partial_content = '{"response":3,"issues":[]}'
            raise exc
        return "\n".join(
            json.dumps({"profile_id": n, "answers": {"1": 1, "2": f"Handoff recorded by reviewer {n}."}})
            for n in task["profile_numbers"]
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    job = Generation(config, None, "", policy={"dependencies": {"2": [1]}})

    with pytest.raises(exc_type, match="Test interruption"):
        job.run()
    saved = Draft.read(job.draft.path).state
    assert saved["review"]["checked"] == [3]
    assert len(saved["profiles"]) == 3
