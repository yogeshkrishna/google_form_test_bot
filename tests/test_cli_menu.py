"""The start menu separates submission recovery from deliberate saved-answer reuse."""

import json

import pytest

from gformbot import cli, previews
from gformbot.config import QuestionConfig, RunConfig
from gformbot.state import Journal, batch_id


@pytest.fixture
def batch_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def create(name="Survey", count=50, *, confirmed=0, state=None, started=False):
        config = RunConfig(
            "https://docs.google.com/forms/d/e/test/viewform",
            form_title=name,
            count=count,
            questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
        )
        path = cli._save_plan(
            config, [{1: f"Exact answer {i}"} for i in range(count)], cleanup_on_success=False
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["submitted_indices"] = list(range(1, confirmed + 1))
        if started:
            payload["submission_started"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        if state:
            Journal(batch_id(payload)).set(confirmed + 1, state)
        return path

    return create


def test_start_menu_hides_continue_for_unstarted_saved_answers(batch_files, monkeypatch):
    batch_files()
    seen = []
    monkeypatch.setattr(cli, "_print_banner", lambda: None)
    monkeypatch.setattr(
        cli, "_ask_select", lambda message, choices, **kw: seen.extend(c.name for c in choices) or "exit"
    )
    cli._wizard()
    assert seen == ["Quick submit", "Start a new form", "More options", "Exit"]


@pytest.mark.parametrize(
    "progress", [{"confirmed": 35}, {"state": "uncertain"}, {"state": "prepared"}, {"started": True}]
)
def test_started_batch_is_available_without_regeneration(batch_files, monkeypatch, progress):
    path = batch_files(**progress)
    selected, _ = cli._saved_batches(started_only=True)
    assert len(selected) == 1 and selected[0][1].path == path.resolve()
    managed = []
    menus = []

    def choose(message, choices, **kw):
        menus.append([choice.name for choice in choices])
        if message == "Start":
            return "exit" if managed else "saved"
        return next(choice.value for choice in choices if isinstance(choice.value, cli.SavedPreview))

    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(
        cli, "_manage_batch", lambda config, answers, path, **kw: managed.append((config, answers, path, kw))
    )
    monkeypatch.setattr(
        cli, "_generate", lambda *a, **kw: pytest.fail("Continuing submission must not regenerate answers")
    )
    cli._wizard()
    assert "Continue response batch" in menus[0]
    assert managed[0][1] == [{1: f"Exact answer {i}"} for i in range(50)]
    assert managed[0][3] == {}


def test_recovery_labels_separate_uncertain_from_safe_remaining(batch_files):
    batch_files(confirmed=35, state="uncertain")
    matches, _ = cli._saved_batches(started_only=True)
    label = cli._batch_label(matches[0][1])
    assert "35/50 submitted" in label
    assert "1 needs checking" in label
    assert "14 remaining" in label


def test_fully_submitted_and_legacy_ai_files_are_not_recovery_jobs(batch_files, tmp_path):
    batch_files(count=2, confirmed=2)
    old_job = tmp_path / "gformbot-generation.json"
    data = '{"kind":"gformbot-generation","responses":[null]}'
    old_job.write_text(data, encoding="utf-8")
    previews.remember(old_job)
    assert cli._saved_batches(started_only=True) == ([], 0)
    assert old_job.read_text(encoding="utf-8") == data
    assert not hasattr(cli, "_resume_generation")


def test_prepared_answers_remain_available_to_own_data(batch_files, monkeypatch):
    path = batch_files(count=2)
    monkeypatch.setattr(
        cli,
        "_ask_select",
        lambda message, choices, **kw: next(
            c.value for c in choices if isinstance(c.value, cli.SavedPreview)
        ),
    )
    selected = cli._select_saved_preview()
    assert selected.path == path.resolve()
    assert selected.answers == [{1: "Exact answer 0"}, {1: "Exact answer 1"}]


def test_more_options_contains_inspection_setup_and_back(monkeypatch):
    choices_seen, calls = [], []
    selections = iter(["more", "inspect", "doctor", "back", "exit"])

    def choose(message, choices, **kw):
        choices_seen.append((message, [choice.name for choice in choices]))
        return next(selections)

    monkeypatch.setattr(cli, "_saved_batches", lambda **kw: ([], 0))
    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(cli, "_url_prompt", lambda: "FORM_URL")
    monkeypatch.setattr(cli, "_run_inspect", lambda url: calls.append(("inspect", url)))
    monkeypatch.setattr(cli.doctor, "callback", lambda: calls.append(("doctor",)))
    cli._wizard()
    assert choices_seen[1] == ("More options", ["View a form's questions", "Check setup", "Back"])
    assert calls == [("inspect", "FORM_URL"), ("doctor",)]


def test_old_ai_jobs_are_not_listed_by_preview(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = tmp_path / "gformbot-generation.json"
    job.write_text('{"kind":"gformbot-generation","responses":[null]}', encoding="utf-8")
    monkeypatch.setattr(cli.click, "launch", lambda *a: pytest.fail("Old AI jobs should not be listed"))
    cli.preview.callback(None)
    assert job.is_file()


def test_quick_submit_backtracks_between_fields_before_submitting(monkeypatch):
    counts = iter([3, 7])
    submitted = []

    def instructions(*_, **__):
        if not submitted:
            submitted.append("back")
            raise cli.PromptBack()
        return "Fictional profiles"

    monkeypatch.setattr(cli, "_url_prompt", lambda **kw: "FORM_URL")
    monkeypatch.setattr(cli, "_ask_number", lambda *a, **kw: next(counts))
    monkeypatch.setattr(cli, "_ask_text", instructions)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **kw: False)
    monkeypatch.setattr(cli, "_submit_command", lambda *a: submitted.append(a))
    cli._quick_submit()
    assert submitted == ["back", ("FORM_URL", 7, "Fictional profiles", None, None, None)]


def test_view_questions_returns_to_answer_sources(monkeypatch):
    config = RunConfig("https://docs.google.com/forms/d/e/test/viewform")
    selections = iter(["inspect", "own"])
    displayed = []
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    monkeypatch.setattr(cli, "_show_form", displayed.append)
    monkeypatch.setattr(cli, "_prepare_source", lambda *a: [{1: "answer"}])
    monkeypatch.setattr(cli, "_save_plan", lambda *a, **kw: "saved.json")
    monkeypatch.setattr(cli, "_manage_batch", lambda *a: None)
    cli._form_choices(config)
    assert displayed == [config]


@pytest.mark.parametrize(("keys", "expected"), [("\r", 1), ("3\r", 3)])
def test_count_is_empty_while_typing_and_enter_uses_default(monkeypatch, keys, expected):
    from InquirerPy import inquirer
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    text = inquirer.text
    monkeypatch.setattr(cli, "_INQUIRER", True)
    with create_pipe_input() as pipe:
        monkeypatch.setattr(inquirer, "text", lambda **kw: text(**kw, input=pipe, output=DummyOutput()))
        pipe.send_text(keys)
        assert cli._ask_number("Number of responses", completed=True) == expected
