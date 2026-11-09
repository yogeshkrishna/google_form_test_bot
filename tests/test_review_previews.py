import json

import pytest
from click.testing import CliRunner
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gformbot import cli, previews
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


def test_preview_changes_stop_execution(batch, tmp_path):
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


def test_broken_preview_index_does_not_hide_current_folder_plans(batch, monkeypatch, tmp_path):
    config, answers = batch
    path = cli._save_plan(config, answers, tmp_path / "plan.json")
    previews.index_path().write_text("broken")
    monkeypatch.chdir(tmp_path)
    assert path in previews.available()


@pytest.mark.parametrize("flag", ["--dry-run", "--submit", "--yes", "--halt"])
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
