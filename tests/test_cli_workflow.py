"""Exercise offline entry points and the saved-batch action tree."""

import json

import pytest
from click.testing import CliRunner
from rich.console import Console

from gformbot import cli, generator, previews
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        url="https://docs.google.com/forms/d/e/test/viewform",
        form_title="Offline form",
        count=2,
        min_delay=0,
        max_delay=0,
        questions=[
            QuestionConfig(1, "Choice", "radio", True, ["A", "B"], entry_id="1"),
            QuestionConfig(2, "Comment", "text", entry_id="2"),
        ],
    )
    rows = [{1: "A", 2: "First"}, {1: "B", 2: "Second"}]
    path = cli._save_plan(config, rows)
    return config, rows, path


def test_startup_saved_batch_needs_no_url_or_form_read(batch, monkeypatch):
    config, rows, path = batch
    choices = iter(["saved"])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **k: next(choices))
    monkeypatch.setattr(cli, "_select_saved_preview", lambda: cli.SavedPreview(path, config, rows))
    monkeypatch.setattr(cli, "_url_prompt", lambda: pytest.fail("offline resume requested a URL"))
    calls = []
    monkeypatch.setattr(cli, "_manage_batch", lambda *a, **k: calls.append((a, k)))
    cli._wizard()
    assert calls == [((config, rows, path), {"saved": True})]


def test_saved_batches_can_list_without_inspecting_form(batch, monkeypatch):
    _, _, path = batch
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **k: pytest.fail("offline listing inspected form"))
    monkeypatch.setattr(cli, "console", Console(width=400))
    selected = []

    def choose(message, choices, **kwargs):
        selected.extend(choices)
        return choices[0].value

    opened = []
    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(cli.click, "launch", lambda value: opened.append(value) or 0)
    result = CliRunner().invoke(cli.cli, ["preview"])
    assert result.exit_code == 0, result.exception
    assert any("Offline form" in choice.name and path.name in choice.name for choice in selected)
    assert opened == [str(path.resolve())]


def test_manage_saved_batch_export_inspect_and_back_preserves_input(batch, monkeypatch):
    config, rows, path = batch
    original = path.read_bytes()
    choices = iter(["export", "inspect", "back"])
    monkeypatch.setattr(cli, "_next_action", lambda **k: next(choices))
    monkeypatch.setattr(cli, "_ask_number", lambda *a, **k: 2)
    config._submitted_indices = [1]
    cli._manage_batch(config, rows, path, saved=True)
    assert path.read_bytes() == original
    assert generator.load_reference(config, path.parent / "gformbot-export.csv") == rows[1:]


def test_edit_creates_new_identity_and_preserves_other_rows(batch, monkeypatch):
    config, rows, path = batch
    original = path.read_bytes()
    choices = iter([2, 2])
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **k: next(choices))
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "Edited")
    fresh, edited, output = cli._edit_batch(config, rows, path)
    assert path.read_bytes() == original
    assert edited == [rows[0], {1: "B", 2: "Edited"}]
    assert fresh.count == 2 and output != path
    assert json.loads(output.read_text())["batch_id"] != json.loads(original)["batch_id"]


def test_optional_written_answers_can_stay_blank_without_ai(batch, monkeypatch):
    config, _, _ = batch
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_reference_prompt", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **k: "auto")
    monkeypatch.setattr(cli, "_generate", lambda *a, **k: pytest.fail("AI was declined"))
    assert all(row[1] in {"A", "B"} and row[2] == "" for row in cli._prepare_source(config, "random"))


def test_optional_csv_mapping_requires_explicit_blank_acknowledgement(batch, monkeypatch):
    config, _, _ = batch
    config.questions[0].required = False
    seen = []

    def choose(message, choices, **kwargs):
        seen.append((message, kwargs.get("default")))
        return "blank"

    monkeypatch.setattr(cli, "_ask_select", choose)
    assert cli._mapping(config, ["Unrelated"]) == {1: None, 2: None}
    assert len(seen) == 1 and seen[0][1] == "map"


def test_no_headless_overrides_saved_background_setting(batch):
    config, _, _ = batch
    config.background = True
    config.headless = True
    config.browser = "chrome"
    cli._apply_browser(config, headless=False)
    assert not config.background and not config.headless


def test_preview_rejects_execution_overrides(batch):
    _, _, path = batch
    result = CliRunner().invoke(cli.cli, ["preview", str(path), "--count", "1"])
    assert result.exit_code == 2 and "No such option" in result.output


def test_completed_batches_are_not_offered_for_execution(batch, monkeypatch):
    _, _, path = batch
    payload = json.loads(path.read_text())
    payload["submitted_indices"] = [1, 2]
    path.write_text(json.dumps(payload), encoding="utf-8")

    def choose(message, choices, **kwargs):
        assert [choice.value for choice in choices] == ["refresh", "back"]
        return "back"

    monkeypatch.setattr(cli, "_ask_select", choose)
    assert cli._select_saved_preview() is None
    assert path.resolve() in previews.available()
