import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from gformbot import ai, cli, generator, inspector, logger, transport
from gformbot.access import SubmissionUncertainError
from gformbot.config import QuestionConfig, RunConfig, save_config


@pytest.fixture
def config():
    return RunConfig(
        url="https://docs.google.com/forms/d/e/test/viewform",
        form_title="Survey",
        count=2,
        sections=["Section 1"],
        min_delay=0,
        max_delay=0,
        questions=[
            QuestionConfig(1, "Activity", "radio", True, ["Study", "Work"], entry_id="11", item_id="21"),
            QuestionConfig(2, "Comment", "text", False, entry_id="12", item_id="22"),
        ],
    )


def profiles():
    return [{1: "Study", 2: "Student"}, {1: "Work", 2: "Worker"}]


def test_plain_menu_reprompts_for_zero_and_invalid_input(monkeypatch):
    monkeypatch.setattr(cli, "_INQUIRER", False)
    values = iter(["0", "bad", "3", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    assert cli._ask_select("Choose", [cli._choice("a", "Alpha"), cli._choice("b", "Beta")]) == "a"


def test_file_picker_opens_only_after_enter_and_closes_hidden_root(monkeypatch, tmp_path):
    events = []
    path = tmp_path / "pool.csv"
    path.write_text("Q1\nStudy\n", encoding="utf-8")

    class Root:
        def withdraw(self):
            events.append("withdraw")

        def attributes(self, *args):
            pass

        def destroy(self):
            events.append("destroy")

    def ask(**kwargs):
        events.append("dialog")
        return str(path)

    monkeypatch.setitem(
        sys.modules, "tkinter", SimpleNamespace(Tk=Root, filedialog=SimpleNamespace(askopenfilename=ask))
    )
    monkeypatch.setattr(cli, "_ask_text", lambda *args: events.append("enter") or "")
    assert cli._select_csv() == path
    assert events == ["enter", "withdraw", "dialog", "destroy"]


@pytest.mark.parametrize("error", [RuntimeError("Tk unavailable"), ValueError("display unavailable")])
def test_picker_failure_recommends_public_flags(monkeypatch, error):
    def unavailable():
        raise error

    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=unavailable, filedialog=None))
    monkeypatch.setattr(cli, "_ask_text", lambda *args: "")
    with pytest.raises(ValueError) as caught:
        cli._select_csv()
    assert "--data" in str(caught.value) and "--like" in str(caught.value)
    assert "--pool" not in str(caught.value) and "--reference" not in str(caught.value)


def test_picker_cancel_returns_to_source_selection(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    choices = iter(["own", "generate", "fresh", "back"])
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(cli, "_url_prompt", lambda: config.url)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    monkeypatch.setattr(cli, "_ask_number", lambda *args, **kwargs: 2)
    calls = []

    def prepare(c, source):
        calls.append(source)
        if source == "own":
            raise cli.FileSelectionCancelled()
        return profiles()

    monkeypatch.setattr(cli, "_prepare_source", prepare)
    cli._new_batch_wizard()
    assert calls == ["own", "fresh"]
    assert (tmp_path / "gformbot-responses.json").is_file()


@pytest.mark.parametrize(
    "source, filename", [("fresh", "gformbot-generated.csv"), ("reference", "gformbot-generated.csv")]
)
def test_generation_sources_write_new_csv_without_changing_reference(
    config, monkeypatch, tmp_path, source, filename
):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(
        cli, "_reference_prompt", lambda c, required=False: seen.append(required) or profiles()
    )
    monkeypatch.setattr(cli, "_ask_text", lambda *args: "Match the style")

    def generate(c, reference, instructions):
        assert reference == (None if source == "fresh" else profiles())
        assert "Match the style" in instructions
        return profiles()

    monkeypatch.setattr(cli, "_generate", generate)
    assert cli._prepare_source(config, source) == profiles()
    assert generator.load_reference(config, tmp_path / filename) == profiles()
    assert seen == ([] if source == "fresh" else [True])


def test_own_data_keeps_original_profiles(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = generator.write_csv(config, profiles(), tmp_path / "pool.csv")
    before = path.read_bytes()
    monkeypatch.setattr(cli, "_select_csv", lambda: path)
    choices = iter(["csv", "ordered"])
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: next(choices))
    assert cli._prepare_source(config, "own") == profiles()
    assert path.read_bytes() == before


def test_random_mixed_form_completes_text_preserving_choice_profiles(config, monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *args: True)
    monkeypatch.setattr(cli, "_reference_prompt", lambda c: profiles())
    monkeypatch.setattr(cli, "_ask_text", lambda *args: "Student language")
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: "auto")

    def complete(c, partials, **kwargs):
        assert len(partials) == 2
        assert kwargs["instructions"] == "Student language"
        return [{**row, 2: "Student" if row[1] == "Study" else "Worker"} for row in partials]

    monkeypatch.setattr(ai, "complete_responses", complete)
    assert all(row in profiles() for row in cli._prepare_source(config, "random"))


def test_random_declining_required_text_generation_returns_to_sources(config, monkeypatch):
    config.questions[1].required = True
    monkeypatch.setattr(cli, "_ask_confirm", lambda *args: False)
    assert cli._prepare_source(config, "random") is None


def test_random_choice_only_requires_no_ai(config, monkeypatch):
    config.questions = config.questions[:1]
    monkeypatch.setattr(cli, "_reference_prompt", lambda c: None)
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: "auto")
    monkeypatch.setattr(ai, "complete_responses", lambda *args, **kwargs: pytest.fail("No AI should run"))
    assert all(row[1] in {"Study", "Work"} for row in cli._prepare_source(config, "random"))


def test_plan_replay_retains_exact_answers_and_rejects_changed_schema(config, tmp_path):
    path = cli._save_plan(config, profiles(), tmp_path / "plan.json")
    restored, rows = cli._load_plan(path)
    assert rows == profiles()
    assert restored.count == 2
    value = json.loads(path.read_text(encoding="utf-8"))
    value["config"]["questions"][0]["text"] = "Different question"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="schema was changed"):
        cli._load_plan(path)


def test_existing_artifacts_are_not_overwritten(config, tmp_path):
    path = cli._save_plan(config, profiles(), tmp_path / "plan.json")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        cli._save_plan(config, profiles(), path)
    assert path.read_bytes() == before


def setup_execution(config, monkeypatch):
    state = SimpleNamespace(calls=[], logs=[], clients=0, closed_clients=0)

    monkeypatch.setattr(inspector, "inspect_form", lambda url: config)
    monkeypatch.setattr(logger, "validate_log_path", lambda *args: None)
    monkeypatch.setattr(logger, "log_result", lambda *args: state.logs.append(args))

    class Client:
        def __init__(self, config):
            state.clients += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            state.closed_clients += 1

        def validate(self, answer, **kwargs):
            state.calls.append(dict(answer))
            return True, "checked"

        def submit(self, answer, before_submit=None, **kwargs):
            if before_submit:
                before_submit()
            state.calls.append(dict(answer))
            return True, "submitted"

    state.client = Client
    monkeypatch.setattr(transport, "Client", Client)
    return state


def test_batch_submits_exact_answers_with_one_http_client(config, monkeypatch):
    state = setup_execution(config, monkeypatch)

    cli._run_submit(config, answers=profiles())
    assert state.clients == state.closed_clients == 1
    assert state.calls == profiles()


def test_false_result_stops_with_honest_count(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    results = iter([(True, "checked"), (False, "Validation failed")])
    monkeypatch.setattr(state.client, "submit", lambda *args, **kwargs: next(results))
    with pytest.raises(ValueError, match="rejected"):
        cli._run_submit(config, answers=profiles())
    assert state.closed_clients == 1
    assert [row[3] for row in state.logs] == [True, False]


def test_uncertain_submit_is_logged_once_and_never_retried(config, monkeypatch):
    state = setup_execution(config, monkeypatch)

    def execute(*args, **kwargs):
        state.calls.append(1)
        raise SubmissionUncertainError("Check the form before retrying")

    monkeypatch.setattr(state.client, "submit", execute)
    with pytest.raises(ValueError, match="Check the form"):
        cli._run_submit(config, answers=profiles())
    assert len(state.calls) == len(state.logs) == 1
    assert state.logs[0][3] is False


def test_log_preflight_happens_before_http_client(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    monkeypatch.setattr(
        logger, "validate_log_path", lambda path: (_ for _ in ()).throw(ValueError("Old log"))
    )
    with pytest.raises(ValueError, match="Old log"):
        cli._run_submit(config, answers=profiles())
    assert state.clients == 0


def test_form_table_shows_full_contents_and_literal_markup(config, monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=220, color_system=None))
    q = config.questions[0]
    q.text = "Very long question " * 8
    q.description = "Question description [literal]"
    q.options = ["A very long option with commas, and brackets [bold]", "Another option"]
    cli._show_form(config)
    output = buffer.getvalue()
    assert "Question description [literal]" in output
    assert "[bold]" in output and "Another option" in output
    assert "Required" in output and "Section 1" in output
    assert "Very long question" in output
    assert "…" not in output


def test_explicit_new_url_does_not_reuse_default_config(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    save_config(config, Path("gformbot_config.yaml"))
    new = RunConfig(url=config.url.replace("test", "other"), questions=config.questions)
    monkeypatch.setattr(cli, "_run_inspect", lambda url, **kwargs: new)
    csv = generator.write_csv(new, profiles(), tmp_path / "input.csv")
    result = CliRunner().invoke(cli.cli, ["prepare", new.url, "--data", str(csv)])
    assert result.exit_code == 0, result.exception
    restored, _ = cli._load_plan(next(tmp_path.glob("gformbot-responses*.json")))
    assert restored.url == new.url


@pytest.mark.parametrize("count", ["0", "501", "-1"])
def test_cli_rejects_invalid_count_before_work(count):
    result = CliRunner().invoke(
        cli.cli, ["generate", "https://docs.google.com/forms/d/e/test/viewform", "--count", count]
    )
    assert result.exit_code == 2


def test_main_error_exit_is_nonzero(monkeypatch):
    def fail(**kwargs):
        raise ValueError("broken input")

    monkeypatch.setattr(cli, "cli", fail)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1


def test_saved_plan_interactive_next_choices(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    target = cli._save_plan(config, profiles())
    monkeypatch.setattr(cli, "interactive_input", lambda: True)
    monkeypatch.setattr(cli, "_next_action", lambda count: "submit")
    calls = []

    def confirmed_fill(c, **kwargs):
        calls.append(kwargs["answers"])
        for index in range(1, len(kwargs["answers"]) + 1):
            c._record_submission(index)
        return True

    monkeypatch.setattr(cli, "_run_submit", confirmed_fill)
    cli._manage_batch(config, profiles(), target)
    assert calls == [profiles()]
    assert not target.exists()


def test_generate_preserves_supplied_plan_and_its_artifacts(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    target = cli._save_plan(config, profiles(), artifacts=[generated])
    calls = []

    def confirmed_fill(c, **kwargs):
        calls.append(kwargs["answers"])
        for index in range(1, len(kwargs["answers"]) + 1):
            c._record_submission(index)
        return True

    monkeypatch.setattr(cli, "_run_submit", confirmed_fill)
    monkeypatch.setattr(cli, "_run_inspect", lambda *args, **kwargs: config)
    result = CliRunner().invoke(cli.cli, ["generate", config.url, "--data", str(target)])
    assert result.exit_code == 0, result.exception
    assert calls == [profiles()]
    assert target.exists() and generated.exists()


@pytest.mark.parametrize("abort", [ValueError("HTTP failed"), click.Abort()])
def test_run_failures_preserve_batch(config, monkeypatch, tmp_path, abort):
    monkeypatch.chdir(tmp_path)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    target = cli._save_plan(config, profiles(), artifacts=[generated])

    def fail(*args, **kwargs):
        raise abort

    monkeypatch.setattr(cli, "_run_submit", fail)
    monkeypatch.setattr(cli, "_run_inspect", lambda *args, **kwargs: config)
    result = CliRunner().invoke(cli.cli, ["generate", config.url, "--data", str(target)])
    assert result.exit_code != 0
    assert target.exists() and generated.exists()


def test_wizard_submits_and_removes_completed_owned_files(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_url_prompt", lambda: config.url)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    monkeypatch.setattr(cli, "_ask_number", lambda *args, **kwargs: 2)
    choices = iter(["generate", "fresh", "submit"])
    defaults = []

    def select(*args, **kwargs):
        defaults.append(kwargs.get("default"))
        return next(choices)

    monkeypatch.setattr(cli, "_ask_select", select)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    config._generated_artifacts = [generated]
    monkeypatch.setattr(cli, "_prepare_source", lambda *args: profiles())
    calls = []

    def confirmed_fill(c, **kwargs):
        calls.append(kwargs["answers"])
        for index in range(1, len(kwargs["answers"]) + 1):
            c._record_submission(index)
        return True

    monkeypatch.setattr(cli, "_run_submit", confirmed_fill)
    cli._new_batch_wizard()
    assert calls == [profiles()]
    assert not generated.exists() and not list(tmp_path.glob("gformbot-responses*.json"))
