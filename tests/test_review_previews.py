import io
import json
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gformbot import browser, cli, filler, inspector, logger, previews
from gformbot import execution as execution_module
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def batch():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        "Review form",
        browser="chrome",
        count=2,
        min_delay=0,
        max_delay=0,
        questions=[QuestionConfig(1, "Name", "text", entry_id="1", item_id="1")],
    )
    return config, [{1: "First"}, {1: "Second"}]


class ReviewDriver:
    def __init__(self):
        self.current_url = "https://docs.google.com/forms/d/e/test/viewform"
        self.current_window_handle = "tab1"
        self.window_handles = ["tab1"]
        self.switch_to = self
        self.confirmed = set()
        self.events = []

    def window(self, handle):
        self.current_window_handle = handle
        self.events.append(("switch", handle))

    def new_window(self, kind):
        assert kind == "tab"
        handle = f"tab{len(self.window_handles) + 1}"
        self.window_handles.append(handle)
        self.window(handle)

    def quit(self):
        self.events.append(("quit",))

    def execute_script(self, script, value=None):
        self.events.append(("title", self.current_window_handle, value))


def execution(config, monkeypatch, mode):
    driver = ReviewDriver()
    monkeypatch.setattr(browser, "get_driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: config)
    monkeypatch.setattr(execution_module, "config_from_schema", lambda value: value)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: mode)
    monkeypatch.setattr(logger, "validate_log_path", lambda *args: None)
    logs = []
    monkeypatch.setattr(logger, "log_result", lambda *args, **kwargs: logs.append(args))

    def fill(c, answer, submit, driver, before_review=None, observe=None):
        assert submit is False
        if before_review:
            before_review()
        driver.events.append(("fill", driver.current_window_handle, answer[1]))
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", fill)
    monkeypatch.setattr(
        filler, "_positive_confirmation", lambda d, *args: d.current_window_handle in d.confirmed
    )
    return driver, logs


def test_tabs_keep_distinct_responses_and_do_not_switch_until_user_checks(batch, monkeypatch):
    config, answers = batch
    driver, logs = execution(config, monkeypatch, "tabs")

    def check(message):
        assert [e for e in driver.events if e[0] == "fill"] == [
            ("fill", "tab1", "First"),
            ("fill", "tab2", "Second"),
        ]
        assert ("quit",) not in driver.events
        driver.confirmed.update(driver.window_handles)
        return ""

    monkeypatch.setattr(cli, "_ask_text", check)
    assert cli._run_fill(config, answers=answers) is True
    assert [row[4] for row in logs] == ["filled", "filled", "submitted manually", "submitted manually"]
    assert driver.events[-1] == ("quit",)


def test_sequential_does_not_fill_next_before_confirmation(batch, monkeypatch):
    config, answers = batch
    driver, logs = execution(config, monkeypatch, "wait")

    def pause(seconds):
        assert len([e for e in driver.events if e[0] == "fill"]) == 1
        driver.confirmed.add("tab1")

    monkeypatch.setattr(time, "sleep", pause)
    assert cli._run_fill(config, answers=answers)
    assert [row[4] for row in logs] == ["filled", "submitted manually", "filled", "submitted manually"]


def test_closed_tab_does_not_discard_other_pending_tabs(batch, monkeypatch):
    config, answers = batch
    driver, logs = execution(config, monkeypatch, "tabs")
    checks = []

    def check(message):
        checks.append(message)
        driver.window_handles = ["tab2"]
        if len(checks) == 2:
            driver.confirmed.add("tab2")
        return ""

    monkeypatch.setattr(cli, "_ask_text", check)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert cli._run_fill(config, answers=answers) is False
    assert len(checks) == 2
    assert [(r[1], r[4]) for r in logs if r[4] == "submitted manually"] == [(2, "submitted manually")]


def test_stop_keeps_plan_and_resume_skips_confirmed_rows(batch, monkeypatch, tmp_path):
    config, answers = batch
    monkeypatch.chdir(tmp_path)
    target = cli._save_plan(config, answers)
    driver, logs = execution(config, monkeypatch, "tabs")
    config._plan_digest = digest(target)
    cli._track_plan(config, answers, target, digest(target))
    selections = iter(["tabs", "stop"])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    checks = []

    def check(message):
        checks.append(message)
        if len(checks) == 1:
            driver.confirmed.add("tab1")
            return ""
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_ask_text", check)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert cli._run_fill(config, answers=answers) is False
    cli._finish_plan(target, config._plan_digest, keep_files=True)
    restored, original = cli._load_plan(target)
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "no")
    assert cli._track_plan(restored, original, target, digest(target)) == [{1: "Second"}]
    assert restored.count == 1
    restored._record_submission(1)
    assert json.loads(target.read_text())["submitted_indices"] == [1, 2]
    restored, original = cli._load_plan(target)
    with pytest.raises(ValueError, match="already confirmed"):
        cli._track_plan(restored, original, target, digest(target))


def test_preview_changes_during_review_stop_execution(batch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    expected = digest(path)
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="changed while"):
        cli._track_plan(config, answers, path, expected)


def test_saved_previews_sorted_by_creation_not_edit_time_and_found_from_other_folder(
    batch, monkeypatch, tmp_path
):
    config, answers = batch
    old = cli._save_plan(config, answers, tmp_path / "old.json")
    new = cli._save_plan(config, answers, tmp_path / "new.json")
    value = json.loads(old.read_text())
    value["created_at"] = "2020-01-01T12:00:00+00:00"
    old.write_text(json.dumps(value))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen = []

    def select(message, choices, **kwargs):
        seen.extend(choices)
        return choices[0].value

    monkeypatch.setattr(cli, "_ask_select", select)
    selected = cli._select_saved_preview(config)
    assert selected.path == new
    assert [c.value.path for c in seen if isinstance(c.value, cli.SavedPreview)] == [new, old]


def test_missing_corrupt_and_other_form_previews_are_skipped(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    path.unlink()
    (tmp_path / "broken.json").write_text("bad")
    config.url = config.url.replace("test", "other")
    cli._save_plan(config, answers, tmp_path / "other.json")
    config.url = config.url.replace("other", "test")
    monkeypatch.chdir(tmp_path)

    def select(message, choices, **kwargs):
        assert not any(isinstance(c.value, cli.SavedPreview) for c in choices)
        return "back"

    monkeypatch.setattr(cli, "_ask_select", select)
    assert cli._select_saved_preview(config) is None


def test_own_data_saved_choice_avoids_file_picker(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    selections = iter(["saved"])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    monkeypatch.setattr(cli, "_select_saved_preview", lambda c: cli.SavedPreview(path, c, answers))
    monkeypatch.setattr(cli, "_select_csv", lambda: pytest.fail("No file picker"))
    selected = cli._prepare_source(config, "own")
    assert selected.path == path and selected.answers == answers


def test_saved_preview_selection_through_real_interactive_menu(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_INQUIRER", True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("\r")
            selected = cli._select_saved_preview(config)
    assert isinstance(selected, cli.SavedPreview)
    assert selected.path == path and selected.answers == answers


def test_interactive_menu_preserves_nested_choice_objects(batch, monkeypatch, tmp_path):
    config, answers = batch
    expected = cli.SavedPreview(tmp_path / "plan.json", config, answers)
    monkeypatch.setattr(cli, "_INQUIRER", True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("\r")
            selected = cli._ask_select("Choose", [cli._choice(expected, "Saved preview")])
    assert selected is expected


def test_run_stopped_review_preserves_files(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    monkeypatch.setattr(cli, "_run_fill", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli, "_run_inspect", lambda *args, **kwargs: config)
    result = CliRunner().invoke(cli.cli, ["generate", config.url, "--data", str(path), "--halt"])
    assert result.exit_code == 0, result.exception
    assert path.exists()


def test_unattended_runs_never_prompt_for_manual_submission(batch, monkeypatch):
    config, _ = batch
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kw: pytest.fail("No prompt"))
    assert cli._runner(config, []).review_mode(False) == "check"


def test_broken_preview_index_does_not_hide_current_folder_plans(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    previews.index_path().write_text("broken")
    monkeypatch.chdir(tmp_path)
    assert path in previews.available()


@pytest.mark.parametrize("flag", ["--dry-run", "--submit", "--yes"])
def test_retired_execution_flags_fail_before_form_access(batch, monkeypatch, flag):
    config, _ = batch
    monkeypatch.setattr(cli, "_run_inspect", lambda *args, **kwargs: pytest.fail("No form access"))
    result = CliRunner().invoke(cli.cli, ["generate", config.url, flag])
    assert result.exit_code == 2 and "No such option" in result.output


@pytest.mark.parametrize("progress", [[0], [3], [True], [1, 1], "bad"])
def test_invalid_submission_progress_is_rejected(batch, tmp_path, progress):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    value = json.loads(path.read_text())
    value["submitted_indices"] = progress
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="progress is invalid"):
        cli._load_plan(path)


def test_saved_preview_menu_does_not_offer_save_again(monkeypatch):
    seen = []

    def select(message, choices, **kwargs):
        seen.extend(c.value for c in choices)
        return "fill"

    monkeypatch.setattr(cli, "_ask_select", select)
    assert cli._next_action(saved=True) == "fill"
    assert seen[:2] == ["fill", "submit"]
    assert "preview" not in seen
    assert {"inspect", "edit", "test", "export"}.issubset(seen)


def test_background_preparation_never_launches_visible_browser_without_open(batch, monkeypatch):
    config, answers = batch
    config.background = True
    driver, logs = execution(config, monkeypatch, "tabs")
    launches = []
    monkeypatch.setattr(browser, "get_driver", lambda browser: launches.append(browser) or driver)
    selections = iter(["tabs", "stop"])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    assert cli._run_fill(config, answers=answers) is False
    assert launches == []
    assert len(driver.window_handles) == 1
    assert not [e for e in driver.events if e[0] == "fill"]


def test_background_open_requires_explicit_choice_and_preserves_edits_on_reopen(batch, monkeypatch):
    config, answers = batch
    config.background = True
    viewer = ReviewDriver()
    selections = iter(["open", 1, "open", 1, "stop"])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    launches, fills = [], []
    monkeypatch.setattr(browser, "get_driver", lambda browser: launches.append(browser) or viewer)
    monkeypatch.setattr(filler, "submit_response", lambda c, a, **kw: fills.append(a) or (True, "filled"))
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: False)
    assert cli._runner(config, answers).background({1: 1, 2: 2}) is False
    assert launches == ["chrome"] and fills == [answers[0]]


def test_background_one_at_a_time_waits_for_manual_confirmation(batch, monkeypatch):
    config, answers = batch
    config.background = True
    viewer = ReviewDriver()
    selections = iter(["open", 2])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: next(selections))
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: viewer)
    monkeypatch.setattr(filler, "submit_response", lambda *a, **kw: (True, "filled"))
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: True)
    recorded = []
    monkeypatch.setattr(logger, "log_result", lambda *a: recorded.append(a[1]))
    pending = {2: 2}
    assert cli._runner(config, answers).background(pending) is True
    assert recorded == [2] and not pending


def test_background_browser_alias_and_visible_override(batch):
    config, _ = batch
    cli._apply_browser(config, "chrome-background")
    assert (config.browser, config.background) == ("chrome", True)
    cli._apply_browser(config, "chrome")
    assert config.browser == "chrome" and not config.background
