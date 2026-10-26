import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from gformbot import ai, browser, cli, filler, generator, inspector, logger
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


def test_picker_cancel_returns_to_source_selection(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    choices = iter(["own", "generate", "fresh", "preview"])
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(cli, "_url_prompt", lambda: config.url)
    monkeypatch.setattr(cli, "_run_inspect", lambda _: config)
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
    "source, filename", [("generate", "gformbot-generated.csv"), ("enlarge", "gformbot-additional.csv")]
)
def test_generation_and_enlarge_write_csv_without_changing_reference(
    config, monkeypatch, tmp_path, source, filename
):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(
        cli, "_reference_prompt", lambda c, required=False: seen.append(required) or profiles()
    )
    monkeypatch.setattr(cli, "_ask_text", lambda *args: "Match the style")

    def generate(c, reference, instructions):
        assert reference == profiles()
        assert "Match the style" in instructions
        if source == "enlarge":
            assert "additional" in instructions
        return profiles()

    monkeypatch.setattr(cli, "_generate", generate)
    assert cli._prepare_source(config, source) == profiles()
    assert generator.load_reference(config, tmp_path / filename) == profiles()
    assert seen == [source == "enlarge"]


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
    state = SimpleNamespace(launches=0, calls=[], logs=[], quit_count=0)

    class Driver:
        current_url = config.url

        def quit(self):
            state.quit_count += 1

        def execute_script(self, *args):
            pass

    def launch(*args, **kwargs):
        state.launches += 1
        return Driver()

    monkeypatch.setattr(browser, "get_driver", launch)
    monkeypatch.setattr(inspector, "inspect_form", lambda url: config)
    monkeypatch.setattr(cli, "config_from_schema", lambda value: value)
    monkeypatch.setattr(logger, "validate_log_path", lambda *args: None)
    monkeypatch.setattr(logger, "log_result", lambda *args: state.logs.append(args))
    return state


def test_batch_reuses_one_browser_and_exact_answers(config, monkeypatch):
    state = setup_execution(config, monkeypatch)

    def execute(c, answer, submit, driver):
        state.calls.append((dict(answer), submit, driver))
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", execute)
    cli._run_fill(config, answers=profiles())
    assert state.launches == state.quit_count == 1
    assert [row for row, _, _ in state.calls] == profiles()
    assert not any(submit for _, submit, _ in state.calls)
    assert state.calls[0][2] is state.calls[1][2]


def test_false_result_stops_with_honest_count(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    results = iter([(True, "filled"), (False, "Validation failed")])
    monkeypatch.setattr(filler, "submit_response", lambda *args, **kwargs: next(results))
    with pytest.raises(ValueError, match="1/2 filled"):
        cli._run_fill(config, answers=profiles())
    assert state.quit_count == 1
    assert [row[3] for row in state.logs] == [True, False]


def test_uncertain_submit_is_logged_once_and_never_retried(config, monkeypatch):
    state = setup_execution(config, monkeypatch)

    def execute(*args, **kwargs):
        state.calls.append(1)
        raise filler.SubmissionUncertainError("Check the form before retrying")

    monkeypatch.setattr(filler, "submit_response", execute)
    with pytest.raises(ValueError, match="Check the form"):
        cli._run_fill(config, answers=profiles(), submit=True)
    assert len(state.calls) == len(state.logs) == 1
    assert state.logs[0][3] is False


def test_dry_run_needs_no_browser_or_live_inspection(config, monkeypatch):
    monkeypatch.setattr(browser, "get_driver", lambda *args, **kwargs: pytest.fail("No browser"))
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: pytest.fail("No live inspection"))
    cli._run_fill(config, answers=profiles(), dry_run=True)


def test_changed_live_form_stops_before_browser(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    different = RunConfig(url=config.url, questions=[], sections=["Different"])
    monkeypatch.setattr(inspector, "inspect_form", lambda url: different)
    with pytest.raises(ValueError, match="has changed"):
        cli._run_fill(config, answers=profiles())
    assert state.launches == 0


def test_log_preflight_happens_before_browser(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    monkeypatch.setattr(
        logger, "validate_log_path", lambda path: (_ for _ in ()).throw(ValueError("Old log"))
    )
    with pytest.raises(ValueError, match="Old log"):
        cli._run_fill(config, answers=profiles())
    assert state.launches == 0


def test_noninteractive_submission_requires_explicit_yes(config, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    with pytest.raises(ValueError, match="confirmation"):
        cli._confirm_submit(config, True)
    cli._confirm_submit(config, True, yes=True)
    cli._confirm_submit(config, False)


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
    monkeypatch.setattr(cli, "_run_inspect", lambda url: new)
    assert cli._resolve_config(new.url).url == new.url


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


def test_visible_rehearsal_waits_for_each_manual_submission(config, monkeypatch):
    state = setup_execution(config, monkeypatch)
    config.browser, config.headless = "chrome", False
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(filler, "submit_response", lambda *args, **kwargs: (True, "filled"))
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: "wait")
    state.driver = None

    def execute(c, answer, **kwargs):
        state.driver = kwargs["driver"]
        state.driver.current_window_handle = "first"
        state.driver.window_handles = ["first"]
        state.calls.append(dict(answer))
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", execute)
    checked = []

    def confirmed(*args):
        checked.append(len(state.calls))
        return True

    monkeypatch.setattr(filler, "_positive_confirmation", confirmed)
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output))
    cli._run_fill(config, answers=profiles())
    assert checked == [1, 2]
    assert state.quit_count == 1
    assert "manual submission confirmed" in output.getvalue()


@pytest.mark.parametrize("action", ["fill", "submit"])
def test_saved_plan_interactive_next_choices(config, monkeypatch, tmp_path, action):
    monkeypatch.chdir(tmp_path)
    target = cli._save_plan(config, profiles())
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_next_action", lambda **kwargs: action)
    monkeypatch.setattr(cli, "_choose_browser", lambda: "chrome")
    monkeypatch.setattr(cli, "_confirm_submit", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        cli, "_run_fill", lambda c, **kwargs: calls.append((c.browser, c.headless, kwargs["submit"]))
    )
    cli.run.callback(
        str(target), browser=None, headless=None, dry_run=False, submit=False, yes=False, keep_files=False
    )
    if action == "preview":
        assert target.exists() and not calls
    else:
        assert calls == [("chrome", False, action == "submit")]
        assert not target.exists()


@pytest.mark.parametrize("keep", [True, False])
def test_run_success_cleanup_and_keep_files(config, monkeypatch, tmp_path, keep):
    monkeypatch.chdir(tmp_path)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    target = cli._save_plan(config, profiles(), artifacts=[generated])
    calls = []
    monkeypatch.setattr(
        cli, "_run_fill", lambda c, **kwargs: calls.append((c.browser, c.headless, kwargs["submit"]))
    )
    args = [str(target), "--browser", "chrome"] + (["--keep-files"] if keep else [])
    result = CliRunner().invoke(cli.run, args)
    assert result.exit_code == 0, result.exception
    assert calls == [("chrome", False, False)]
    assert target.exists() == generated.exists() == keep


@pytest.mark.parametrize("abort", [ValueError("Browser failed"), click.Abort()])
def test_run_failures_preserve_batch(config, monkeypatch, tmp_path, abort):
    monkeypatch.chdir(tmp_path)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    target = cli._save_plan(config, profiles(), artifacts=[generated])

    def fail(*args, **kwargs):
        raise abort

    monkeypatch.setattr(cli, "_run_fill", fail)
    result = CliRunner().invoke(cli.run, [str(target), "--headless"])
    assert result.exit_code != 0
    assert target.exists() and generated.exists()


def test_wizard_visible_default_and_completed_files_removed(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_url_prompt", lambda: config.url)
    monkeypatch.setattr(cli, "_run_inspect", lambda _: config)
    monkeypatch.setattr(cli, "_ask_number", lambda *args, **kwargs: 2)
    choices = iter(["generate", "fresh", "fill", "chrome"])
    defaults = []

    def select(*args, **kwargs):
        defaults.append(kwargs.get("default"))
        return next(choices)

    monkeypatch.setattr(cli, "_ask_select", select)
    generated = generator.write_csv(config, profiles(), tmp_path / "generated.csv")
    config._generated_artifacts = [generated]
    monkeypatch.setattr(cli, "_prepare_source", lambda *args: profiles())
    calls = []
    monkeypatch.setattr(cli, "_run_fill", lambda c, **kwargs: calls.append((c.browser, c.headless)))
    cli._new_batch_wizard()
    assert defaults[-1] == "auto-background"
    assert calls == [("chrome", False)]
    assert not generated.exists() and not list(tmp_path.glob("gformbot-responses*.json"))
