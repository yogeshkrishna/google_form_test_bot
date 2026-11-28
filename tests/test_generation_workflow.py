"""Exercise form context, deterministic quality, repair, and review recovery through the CLI."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from gformbot import ai_draft, chatgpt_http, cli, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation import _REVIEW_BYTES, Generation, ReviewBlockedError, _payload_size


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


def test_short_reviews_share_one_request_across_routes(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/quality/viewform",
        count=5,
        sections=["Start", "Undergraduate", "Employed", "Job seeker", "Employer"],
        questions=[
            QuestionConfig(page, f"Section {page} feedback", "textarea", page=page) for page in range(2, 6)
        ],
    )
    job = Generation(config, None, "")
    monkeypatch.setattr(job, "_save", lambda: None)
    profiles = {
        number: {"route": [1, page], "answers": {page: f"Feedback {number}"}, "review_keys": [page]}
        for number, page in enumerate([2, 3, 4, 5, 2], 1)
    }
    requests = []

    def ask(messages):
        task = json.loads(messages[-1]["content"])
        requests.append(task)
        for number, profile in task["profiles"].items():
            assert profile["route"] == profiles[int(number)]["route"]
            assert set(profile["answers"]) == {str(profile["route"][-1])}
        return "\n".join(json.dumps({"response": int(number), "issues": []}) for number in task["profiles"])

    job._review(ask, [], profiles)
    assert sorted(job.review["checked"]) == [1, 2, 3, 4, 5]
    assert len(requests) == 1
    assert set(requests[0]["form"]["questions"]) == {"2", "3", "4", "5"}


def test_oversized_review_keeps_the_selected_answer_and_context(monkeypatch):
    options = ["Site tour"] + [f"Unselected option {i}: " + "x" * 300 for i in range(100)]
    config = RunConfig(
        "https://docs.google.com/forms/d/e/large-review/viewform",
        "Workshop",
        form_description="Report your experience at the workshop.",
        sections=["Start", "Site tour"],
        instructions=[{"page": 2, "text": "Describe the sign you saw."}],
        questions=[
            QuestionConfig(1, "Which activity?", "radio", options=options, branches={"Site tour": 2}),
            QuestionConfig(2, "What was on the sign?", "text", page=2, description="Quote the word shown."),
        ],
    )
    evidence = "The sign reads Open."
    job = Generation(config, None, "Keep the answer short.", policy={"evidence": {"2": evidence}})
    monkeypatch.setattr(job, "_save", lambda: None)
    profile = {
        "route": [1, 2],
        "answers": {1: "Site tour", 2: "Open"},
        "facts": {"1": "Site tour"},
        "review_keys": [2],
    }
    requests = []

    def ask(messages):
        assert _payload_size(messages) <= _REVIEW_BYTES
        task = json.loads(messages[-1]["content"])
        form = task["form"]
        assert form["title"] == "Workshop"
        assert form["description"] == config.form_description
        assert form["instructions"] == "Keep the answer short."
        assert form["route_sections"] == {"1": "Start", "2": "Site tour"}
        assert form["section_instructions"] == config.instructions
        assert form["questions"] == {
            "1": {"text": "Which activity?", "description": "", "page": 1},
            "2": {"text": "What was on the sign?", "description": "Quote the word shown.", "page": 2},
        }
        assert form["brief"]["supplied_evidence"] == {"2": evidence}
        assert task["profiles"]["1"] == json.loads(json.dumps(profile))
        requests.append(messages[0]["content"])
        return '{"response":1,"issues":[]}' if len(requests) == 2 else ""

    job._review(ask, [], {1: profile})
    assert len(requests) == 2
    assert "The previous reply could not be validated" in requests[1]
    assert job.review["checked"] == [1]
    assert config.questions[0].options == options


def test_review_still_rejects_profiles_that_exceed_the_compact_budget(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/large-review/viewform",
        questions=[QuestionConfig(1, "Notes", "textarea")],
    )
    job = Generation(config, None, "")
    monkeypatch.setattr(job, "_save", lambda: None)
    rows = [{1: "x" * _REVIEW_BYTES}]
    profiles = {1: {"route": [1], "answers": rows[0], "review_keys": [1]}}

    def ask(_):
        pytest.fail("An oversized review request reached the provider.")

    with pytest.raises(ReviewBlockedError, match="exceeds the prompt budget"):
        job._review(ask, rows, profiles)
    assert job.review["checked"] == []


def test_incomplete_first_review_does_not_discard_later_valid_rows(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/incomplete-review/viewform",
        questions=[QuestionConfig(1, "Notes", "textarea")],
        count=2,
    )
    job = Generation(config, None, "")
    monkeypatch.setattr(job, "_save", lambda: None)
    rows = [{1: "a" * 14_000}, {1: "b" * 14_000}]
    profiles = {
        number: {"route": [1], "answers": row, "review_keys": [1]} for number, row in enumerate(rows, 1)
    }
    calls = []

    def ask(messages):
        ids = list(json.loads(messages[-1]["content"])["profiles"])
        calls.append(ids)
        return '{"response":2,"issues":[]}' if ids == ["2"] else ""

    with pytest.raises(ValueError, match="Quality review returned incomplete results"):
        job._review(ask, rows, profiles)
    assert calls == [["1"], ["1"], ["2"]]
    assert job.review["checked"] == [2]
    assert not job.review["issues"]


def test_experimental_review_references_migrate_on_next_draft_save(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [QuestionConfig(1, "State", "text"), QuestionConfig(2, "Report", "textarea")],
    )
    issue = {"response": 1, "question": 2, "reason": "State and report differ.", "blocking": True}
    state = {
        "strategy": "flat",
        "profiles": [{"answers": {"1": "Offline", "2": "Running"}, "position": 1, "route": None}],
        "review": {"checked": [1], "issues": [issue]},
        "setup": {},
    }
    draft = Draft.create(config, None, "", None, state, options={"allow_partial": True})
    legacy = json.loads(draft.path.read_bytes())
    del legacy["checksum"]
    legacy["state"]["review"]["issues"][0]["related"] = [1]
    legacy["checksum"] = ai_draft._digest(legacy)
    draft.path.write_text(json.dumps(legacy), encoding="utf-8")
    before = draft.path.read_bytes()

    loaded = Draft.read(draft.path)
    assert loaded.path.read_bytes() == before
    assert loaded.state["review"]["issues"] == [issue]
    assert loaded.state["setup"]["review_related"] == {"1": {"2": [1]}}
    loaded.save(loaded.state)

    persisted = json.loads(draft.path.read_bytes())
    assert persisted["version"] == 2
    assert persisted["identity"] == draft.identity
    assert persisted["state"]["review"]["issues"] == [issue]
    assert persisted["state"]["setup"]["review_related"] == {"1": {"2": [1]}}
    assert Draft.read(draft.path).options == {"allow_partial": True}


def test_oversized_repair_preserves_cited_source_answers(monkeypatch):
    options = ["Offline", "Online", "x" * _REVIEW_BYTES]
    config = RunConfig(
        "https://docs.google.com/forms/d/e/large-repair/viewform",
        questions=[
            QuestionConfig(1, "Endpoint state", "radio", options=options),
            QuestionConfig(2, "Summarize the endpoint check", "textarea", required=True),
            QuestionConfig(3, "Unrelated notes", "textarea"),
        ],
    )
    job = Generation(config, None, "")
    monkeypatch.setattr(job, "_save", lambda: None)
    monkeypatch.setattr(job, "_remember_row", lambda *_: None)
    rows = [{1: "Offline", 2: "The endpoint is online.", 3: "Notes kept as entered."}]
    profiles = {1: {"route": [1], "answers": dict(rows[0]), "facts": {}, "review_keys": [2, 3]}}
    calls = []

    def ask(messages):
        assert _payload_size(messages) <= _REVIEW_BYTES
        payload = json.loads(messages[-1]["content"])
        if "repair" in payload:
            calls.append("repair")
            assert payload["profile"]["answers"] == {"1": "Offline", "2": "The endpoint is online."}
            assert list(payload["questions"]) == ["1", "2"]
            assert [question["index"] for question in payload["repair_questions"]] == [2]
            return '{"profile_id":1,"answers":{"2":"The endpoint is offline."}}'
        calls.append("review")
        if payload["profiles"]["1"]["answers"]["2"] == "The endpoint is offline.":
            return '{"response":1,"issues":[]}'
        return json.dumps(
            {
                "response": 1,
                "issues": [
                    {
                        "question": 2,
                        "kind": "contradiction",
                        "reason": "The summary contradicts the recorded state.",
                        "evidence": [{"question": 2, "quote": "online"}, {"question": 1, "quote": "Offline"}],
                    }
                ],
            }
        )

    job._review(ask, rows, profiles)
    assert calls == ["review", "repair", "review"]
    assert rows == [{1: "Offline", 2: "The endpoint is offline.", 3: "Notes kept as entered."}]
    assert not job.review["issues"]


@pytest.mark.parametrize(
    ("target_count", "omit_records", "count"),
    [(3, False, 100), (8, False, 100), (3, True, 100), (8, False, 5)],
)
def test_review_batches_grow_after_complete_replies_and_retry_only_missing(
    monkeypatch, target_count, omit_records, count
):
    keys = range(1, target_count + 1)
    config = RunConfig(
        "https://docs.google.com/forms/d/e/adaptive-review/viewform",
        count=count,
        questions=[QuestionConfig(key, f"Workshop feedback {key}", "textarea") for key in keys],
    )
    job = Generation(config, None, "")
    saved = set()
    monkeypatch.setattr(job, "_save", lambda: saved.update(job.review["checked"]))
    profiles = {
        number: {
            "route": [1],
            "answers": {key: "The workshop was useful." for key in keys},
            "review_keys": list(keys),
        }
        for number in range(1, count + 1)
    }
    requests = []
    omitted = []
    first_omission = None

    def ask(messages):
        nonlocal first_omission
        task = json.loads(messages[-1]["content"])
        ids = tuple(map(int, task["profiles"]))
        assert not saved.intersection(ids)
        requests.append(ids)
        if omit_records and first_omission is None and len(ids) > 8:
            first_omission = len(requests) - 1
            omitted.extend(ids[-3:])
            ids = ids[:-3]
        return "\n".join(json.dumps({"response": number, "issues": []}) for number in ids)

    job._review(ask, [], profiles)
    assert saved == set(profiles)
    assert len(job.review["checked"]) == count
    assert len(requests) <= (1 if count == 5 else 10 if omit_records else 8)
    if omit_records:
        assert first_omission is not None
        first_retry, second_retry, next_group = requests[first_omission + 1 : first_omission + 4]
        assert first_retry + second_retry == tuple(omitted)
        assert len(next_group) <= len(requests[first_omission]) // 2


def test_explicit_resume_gets_a_bounded_retry_for_an_exhausted_review(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Handoff notes", "textarea", True),
            QuestionConfig(2, "Power", "radio", True, ["On", "Off"]),
        ],
        count=2,
    )
    calls = []
    original = "The original notes need correction."
    corrected = "The revised notes clearly record the handoff."
    other = "The second report already records the handoff."
    can_repair = False

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        if "repair_questions" in task:
            calls.append(("repair", tuple(task["profile_numbers"])))
            return json.dumps({"profile_id": 1, "answers": {"1": corrected if can_repair else original}})
        if "profiles" in task:
            calls.append(("review", tuple(map(int, task["profiles"]))))
            return "\n".join(
                json.dumps(
                    {
                        "response": int(number),
                        "issues": [
                            {
                                "question": 1,
                                "kind": "contradiction",
                                "reason": "The handoff must be recorded clearly.",
                                "evidence": [{"question": 1, "quote": original}],
                            }
                        ]
                        if profile["answers"]["1"] == original
                        else [],
                    }
                )
                for number, profile in task["profiles"].items()
            )
        calls.append(("generate", tuple(task["profile_numbers"])))
        return "\n".join(
            json.dumps({"profile_id": number, "answers": {"1": original if number == 1 else other, "2": 1}})
            for number in task["profile_numbers"]
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    runner = CliRunner()
    interrupted = runner.invoke(cli.cli, ["prepare", config.url, "-n", "2"])
    assert interrupted.exit_code == 1 and "needs review" in str(interrupted.exception)
    assert calls.count(("repair", (1,))) == 2
    (path,) = (tmp_path / "state" / "ai-drafts").glob("*.json")
    draft = Draft.read(path)
    assert draft.state["review"]["checked"] == [1, 2]
    assert draft.state["setup"]["repair_attempts"] == {"1": 2}

    calls.clear()
    can_repair = True
    result = runner.invoke(cli.cli, ["prepare", "--resume", str(path)])
    assert result.exit_code == 0, result.exception
    assert calls == [("repair", (1,)), ("review", (1,))]
    assert _plan(tmp_path)["responses"] == [{"1": corrected, "2": "On"}, {"1": other, "2": "On"}]


@pytest.mark.parametrize("phase", ["generation", "review"])
def test_interrupted_ai_draft_resumes_from_saved_batches(tmp_path, monkeypatch, phase):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "Equipment state", "radio", True, ["On", "Off"]),
            QuestionConfig(2, "Handoff notes", "textarea", True),
        ],
        count=2,
    )
    calls = []
    original = {1: "On", 2: "The equipment can stay on for the next shift."}

    def ask(self, messages, **kwargs):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            stage = "review"
            reply = {
                int(number): json.dumps({"response": int(number), "issues": []})
                for number in task["profiles"]
            }
        else:
            stage = "generation"
            reply = {
                number: json.dumps({"profile_id": number, "answers": {"1": 1, "2": original[2]}})
                for number in task["profile_numbers"]
            }
        call = (stage, tuple(reply))
        interrupt = stage == phase and call == (phase, (1, 2)) and call not in calls
        calls.append(call)
        if interrupt:
            error = KeyboardInterrupt()
            error.partial_content = reply[1] + '\n{"truncated":'
            raise error
        return "\n".join(reply.values())

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "-n", "2"])
    assert result.exit_code == 1 and "Open Continue batches" in result.output
    assert all(isinstance(batch, cli.SavedDraft) for _, batch in cli._saved_batches()[0])
    opened = []
    messages = []

    def select(_, choices, **kwargs):
        choice = next(item for item in choices if isinstance(item.value, cli.SavedDraft))
        expected = "1/2 generated" if phase == "generation" else "1/2 quality checked"
        assert choice.name.startswith(expected + " | ")
        assert config.form_title not in choice.name
        return choice.value

    def inspect(url, *, display):
        assert messages[-1] == f"Form: {config.form_title}"
        assert url == config.url and not display
        return deepcopy(config)

    monkeypatch.setattr(cli, "_ask_select", select)
    monkeypatch.setattr(cli, "_info", messages.append)
    monkeypatch.setattr(cli, "_run_inspect", inspect)
    monkeypatch.setattr(cli, "_manage_batch", lambda config, rows, path: opened.append(rows))
    cli._resume_batch()
    assert (phase, (2,)) in calls
    assert calls.count((phase, (1, 2))) == 1
    assert opened == [[original, original]]
    assert not list((tmp_path / "state" / "ai-drafts").glob("*.json"))


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
                                "kind": "cross_answer",
                                "reason": "Q1 selects carrot and Q2 specifies six.",
                                "evidence": [
                                    {"question": 3, "quote": "four chocolate"},
                                    {"question": 1, "quote": "Carrot"},
                                    {"question": 2, "quote": "6"},
                                ],
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


def test_answered_optional_media_can_be_resolved_without_regenerating_saved_rows(tmp_path, monkeypatch):
    config = _setup(
        monkeypatch,
        tmp_path,
        [
            QuestionConfig(1, "What is shown in the image?", "radio", options=["Circle", "Square"]),
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
