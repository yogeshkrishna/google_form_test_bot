"""Generation preserves presets and resumes validated large-form drafts."""

import json
from copy import deepcopy
from io import StringIO
from threading import Event

import pytest
from click.testing import CliRunner
from rich.console import Console

from gformbot import ai, ai_partition, chatgpt_http, cli, previews, terminal
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


def test_terminal_generation_keeps_one_timer_through_quality_review(generation_config, monkeypatch):
    generation_config.count = 25
    generation_config.questions[1].qtype = "textarea"
    output = StringIO()
    monkeypatch.setenv("TERM", "xterm")
    console = Console(
        file=output,
        force_terminal=True,
        legacy_windows=False,
        color_system="standard",
        no_color=False,
        width=100,
    )
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(cli, "interactive_input", lambda stream: True)
    now = 0
    monkeypatch.setattr(terminal, "monotonic", lambda: now)

    def ask(self, messages, *, timeout):
        nonlocal now
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            now += 3
            return _clear_review(task)
        now += 6
        return "\n".join(
            json.dumps({"profile_id": number, "answers": {"1": 1, "2": f"Written answer {number}"}})
            for number in task["profile_numbers"]
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert len(cli._generate(generation_config)) == 25
    transcript = output.getvalue()
    assert "\x1b[2K" in transcript
    assert "Creating answers: 20/25 saved" in transcript
    assert "Quality checked: 0/25 saved" in transcript
    assert "Elapsed 00:12" in transcript
    assert "ETA" in transcript
    assert "\x1b[90mCreating answers: 25/25 saved" in transcript
    assert "Quality checked: 25/25 saved" in transcript
    final_display, _, _ = transcript.rpartition("\x1b[?25h")
    final_display = final_display.rsplit("Creating answers:", 1)[-1]
    assert f"Elapsed 00:{now:02d}" in final_display
    assert "ETA" not in final_display


def test_plain_generation_reports_its_phase_before_a_provider_reply(generation_config, monkeypatch):
    generation_config.questions[1].qtype = "textarea"
    messages_seen = []
    heartbeat = Event()

    def report(message):
        messages_seen.append(message)
        if message == "Creating answers · Elapsed 00:23":
            heartbeat.set()

    monkeypatch.setattr(cli, "console", Console(file=StringIO()))
    monkeypatch.setattr(cli, "_info", report)
    now = 0
    monkeypatch.setattr(terminal, "monotonic", lambda: now)
    monkeypatch.setattr(terminal, "_PROGRESS_HEARTBEAT_SECONDS", 0.01)

    def ask(self, messages, *, timeout):
        nonlocal now
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            assert "Checking linked answers · Elapsed 00:23" in messages_seen
            return _clear_review(task)
        assert "Creating answers · Elapsed 00:00" in messages_seen
        now = 23
        assert heartbeat.wait(2)
        return "\n".join(
            json.dumps({"profile_id": number, "answers": {"1": 1, "2": f"Answer {number}"}})
            for number in task["profile_numbers"]
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert len(cli._generate(generation_config)) == 2
    assert "Creating answers: 2/2 saved" in messages_seen


def test_eta_uses_only_saved_work_in_the_active_stage(monkeypatch):
    now = 0
    monkeypatch.setattr(terminal, "monotonic", lambda: now)
    view = terminal.GenerationProgress(Console(), 10)
    monkeypatch.setattr(view._live, "start", lambda **kwargs: None)
    monkeypatch.setattr(view._live, "refresh", lambda: None)
    view.__enter__()

    now = 16
    view.answers(8, 10)
    assert view._render().renderables[-1].plain == "Elapsed 00:16  |  Answers ETA ~00:04"

    now = 20
    view.answers(10, 10)
    assert view._render().renderables[-1].plain == "Elapsed 00:20"

    now = 22
    view.update("review", 3, 9)
    assert view._render().renderables[-1].plain == "Elapsed 00:22  |  Review ETA estimating..."

    now = 26
    view.update("review", 5, 9)
    assert view._render().renderables[-1].plain == "Elapsed 00:26  |  Review ETA ~00:08"

    view.update("review", 4, 9)
    assert view._render().renderables[-1].plain == "Elapsed 00:26  |  Review ETA estimating..."


def test_partition_eta_uses_successive_field_counts(monkeypatch):
    now = 0
    monkeypatch.setattr(terminal, "monotonic", lambda: now)
    view = terminal.GenerationProgress(Console(), 10, saved=1)
    monkeypatch.setattr(view._live, "start", lambda **kwargs: None)
    monkeypatch.setattr(view._live, "refresh", lambda: None)
    view.__enter__()

    view.update("fields", 20, 100)
    now = 8
    view.update("fields", 40, 100)
    view.answers(3, 10)
    assert view._render().renderables[-1].plain == "Elapsed 00:08  |  Fields ETA ~00:24"


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


@pytest.mark.parametrize("source", ["fresh", "reference", "random", "prepare", "quick"])
def test_media_observations_use_shared_generation(generation_config, monkeypatch, source):
    config = generation_config
    config.count = 1
    config.questions[1].text = "What is shown in the image?"
    instructions = "Keep it short; Q2: The image shows a green triangle."
    monkeypatch.setattr(cli, "_ask_text", lambda *a: instructions)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a: True)
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "auto")
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a, **kw: [{1: "A", 2: "Guide answer"}])
    monkeypatch.setattr(cli, "_run_submit", lambda *a, **kw: True)
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        calls.append("review" if "profiles" in task else "generate")
        if "profiles" in task:
            assert task["form"]["brief"]["supplied_evidence"] == {"2": "The image shows a green triangle."}
            return _clear_review(task)
        assert task["instructions"] == "Keep it short"
        assert task["generation_brief"]["supplied_evidence"] == {"2": "The image shows a green triangle."}
        return '{"profile_id":1,"answers":{"1":1,"2":"Green triangle."}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    if source == "prepare":
        result = CliRunner().invoke(cli.cli, ["prepare", config.url, "-m", instructions])
        assert result.exit_code == 0, result.exception
    elif source == "quick":
        monkeypatch.setattr(cli, "_url_prompt", lambda **kw: config.url)
        monkeypatch.setattr(cli, "_ask_number", lambda *a, **kw: 1)
        monkeypatch.setattr(cli, "_ask_text", lambda *a, **kw: instructions)
        monkeypatch.setattr(cli, "_quick_reference", lambda: None)
        cli._quick_submit()
    else:
        assert cli._prepare_source(config, source) == [{1: "A", 2: "Green triangle."}]
    assert calls == ["generate", "review"]


def test_quick_and_normal_pass_same_guide_and_instructions_to_generation(generation_config, monkeypatch):
    config = generation_config
    config.count = 1
    reference_file = cli.Path("guide.csv")
    reference_file.write_text("Q1,Q2\nB,Traveling by train\n", encoding="utf-8")
    messages = []
    instructions = "Keep each reason brief."

    def generate(config, reference, message, **options):
        messages.append((reference, message, options))
        return [{1: "A", 2: "Walking to work"}]

    monkeypatch.setattr(cli, "_select_csv", lambda title: reference_file)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **kw: instructions)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: deepcopy(config))
    monkeypatch.setattr(cli, "_generate", generate)
    monkeypatch.setattr(cli, "_run_submit", lambda *a, **kw: True)

    cli._prepare_source(config, "reference")
    monkeypatch.setattr(cli, "_url_prompt", lambda **kw: config.url)
    monkeypatch.setattr(cli, "_ask_number", lambda *a, **kw: 1)
    monkeypatch.setattr(cli, "_quick_reference", lambda: reference_file)
    cli._quick_submit()

    reference = [{1: "B", 2: "Traveling by train"}]
    assert messages == [(reference, instructions, {})] * 2


def test_guided_random_completion_does_not_preset_inactive_questions(generation_config, monkeypatch):
    config = generation_config
    config.count = 1
    config.sections = ["Start", "Skipped", "Chosen"]
    config.questions = [
        QuestionConfig(1, "Route", "radio", True, ["Go"], branches={"Go": 3}),
        QuestionConfig(2, "Inactive checkbox", "checkbox", True, ["Choice"], page=2),
        QuestionConfig(3, "Reason", "text", True, page=3),
    ]
    reference = [{1: "Go", 2: [], 3: "CSV example"}]
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a: True)
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "auto")
    monkeypatch.setattr(cli, "_ask_text", lambda *a: "")
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a: reference)

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        assert task["partial_profiles"] == [{"1": 1}]
        return '{"profile_id":1,"answers":{"1":1,"2":[],"3":"Generated reason"}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    assert cli._prepare_source(config, "random") == [{1: "Go", 2: [], 3: "Generated reason"}]
    assert reference == [{1: "Go", 2: [], 3: "CSV example"}]


def test_missing_media_stops_before_generating_written_answers(generation_config, monkeypatch):
    config = generation_config
    config.count = 1
    config.questions[1].text = "What is shown in the image?"
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    monkeypatch.setattr(
        chatgpt_http.Client, "ask", lambda *a, **kw: pytest.fail("No model call needed to find missing media")
    )
    result = CliRunner().invoke(cli.cli, ["prepare", config.url])
    assert result.exit_code == 1 and "Add Q2: your observation" in str(result.exception)
    assert len(list(previews.index_path().with_name("ai-drafts").glob("*.json"))) == 1


def test_explicit_quality_evidence_takes_precedence_over_inline_observations(
    generation_config, monkeypatch, tmp_path
):
    config = generation_config
    config.count = 1
    config.questions[1].text = "What is shown in the image?"
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    policy = tmp_path / "quality.json"
    policy.write_text('{"evidence":{"Q2":"The image shows a square."}}', encoding="utf-8")

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            return _clear_review(task)
        assert task["generation_brief"]["supplied_evidence"] == {"2": "The image shows a square."}
        return '{"profile_id":1,"answers":{"1":1,"2":"Square."}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(
        cli.cli, ["prepare", config.url, "-m", "Q2: a triangle", "--quality", str(policy)]
    )
    assert result.exit_code == 0, result.exception


def test_unselected_branch_checks_media_after_route_and_before_answers(generation_config, monkeypatch):
    config = generation_config
    config.count = 1
    config.sections = ["Start", "Show image", "Without image"]
    config.section_next = [2, 0, 0]
    config.questions = [
        QuestionConfig(
            1, "Route", "radio", True, ["With image", "Without image"], branches={"Without image": 3}
        ),
        QuestionConfig(2, "What is shown in the image?", "text", True, page=2),
        QuestionConfig(3, "Reason", "text", True, page=3),
    ]
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        calls.append([q["key"] for q in task["questions"]])
        assert calls == [["1"]], "the routed media question needs observations, not guesses"
        return '{"profile_id":1,"answers":{"1":1}}'

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    with pytest.raises(ai.GenerationError, match="Add Q2: your observation"):
        cli._generate(config)
    assert calls == [["1"]]


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


@pytest.mark.parametrize("initial_error", ["invalid value", "missing key"])
def test_generation_batches_keep_their_size_when_answers_need_repair(
    generation_config, monkeypatch, initial_error
):
    generation_config.count = 100
    generation_config.questions[1].validation = [[6, 202, ["30"]]]
    calls, seen = [], set()

    def generate(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        ids = task["profile_numbers"]
        repairs = task.get("repair_fields", {})
        calls.append((ids, repairs))
        records = []
        for number in ids:
            # The live model treated a mixed request as a request for patches only.
            if repairs and str(number) not in repairs:
                continue
            keys = repairs.get(str(number), ("1", "2"))
            answers = {key: 1 if key == "1" else f"Reason {number}" for key in keys}
            if number % 10 == 0 and number not in seen:
                if initial_error == "invalid value":
                    answers["2"] = "x" * 80
                else:
                    del answers["2"]
            records.append(json.dumps({"profile_id": number, "answers": answers}))
        seen.update(ids)
        return "\n".join(records)

    monkeypatch.setattr(chatgpt_http.Client, "ask", generate)
    rows = ai.generate_responses(generation_config, 100)
    assert rows == [{1: "A", 2: f"Reason {number}"} for number in range(1, 101)]
    call_limit = 6 if initial_error == "invalid value" else 7
    assert len(calls) <= call_limit, [len(ids) for ids, _ in calls]
    assert [len(ids) for ids, _ in calls[:5]] == [20] * 5
    assert all(not repairs or set(repairs) == {str(i) for i in ids} for ids, repairs in calls)


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
    assert result.exit_code != 0
    assert any(line.startswith("gformbot prepare --resume ") for line in result.output.splitlines())
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
