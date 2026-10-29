"""Public command contracts, using local data and mocked browser/AI boundaries."""

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from gformbot import browser, cli, generator
from gformbot.config import QuestionConfig, RunConfig, config_to_dict, save_config
from gformbot.state import Journal, batch_id


@pytest.fixture
def commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        url="https://docs.google.com/forms/d/e/fixture/viewform",
        form_title="Command fixture",
        questions=[
            QuestionConfig(1, "Choice", "radio", True, ["A", "B"], entry_id="1", item_id="11"),
            QuestionConfig(2, "Reason", "text", True, entry_id="2", item_id="22"),
        ],
        min_delay=0,
        max_delay=0,
    )
    state = SimpleNamespace(config=config, inspected=[], generated=[], filled=[], opened=[])

    def inspect(url, **kwargs):
        state.inspected.append((url, kwargs))
        inspected = copy.deepcopy(config)
        inspected.url = url
        return inspected

    def generate(config, reference=None, instructions="", **kwargs):
        state.generated.append((copy.deepcopy(config), copy.deepcopy(reference), instructions, kwargs))
        return [
            {1: "A" if index % 2 == 0 else "B", 2: f"Generated {index + 1}"} for index in range(config.count)
        ]

    def fill(config, **kwargs):
        state.filled.append((copy.deepcopy(config), copy.deepcopy(kwargs)))
        return True

    monkeypatch.setattr(cli, "_run_inspect", inspect)
    monkeypatch.setattr(cli, "_generate", generate)
    monkeypatch.setattr(cli, "_run_fill", fill)
    monkeypatch.setattr(
        cli.click, "launch", lambda filename, **kwargs: state.opened.append(Path(filename)) or 0
    )
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **kw: pytest.fail("Scripted commands need no extra menu")
    )
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: pytest.fail("Unexpected selection menu"))
    return state


def invoke(*args):
    return CliRunner().invoke(cli.cli, list(args))


def profiles():
    return [{1: "B", 2: "First complete profile"}, {1: "A", 2: "Second complete profile"}]


def saved_plan(commands, tmp_path, *, submitted=()):
    config = copy.deepcopy(commands.config)
    config.count = 2
    path = cli._save_plan(config, profiles(), tmp_path / "saved.json", cleanup_on_success=False)
    if submitted:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["submitted_indices"] = list(submitted)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_public_help_has_only_the_streamlined_commands():
    assert set(cli.cli.commands) == {"generate", "prepare", "preview", "config", "log", "doctor"}
    result = invoke("--help")
    assert result.exit_code == 0
    assert all(name in result.output for name in ("generate", "prepare", "preview", "doctor"))


def test_doctor_strict_check_rejects_missing_browser(monkeypatch):
    monkeypatch.setattr(browser, "available_browsers", lambda: [])
    informational = invoke("doctor")
    assert informational.exit_code == 0
    assert "None detected" in informational.output
    strict = invoke("doctor", "--check")
    assert strict.exit_code == 1
    assert "Setup incomplete" in strict.output and "a supported browser" in strict.output


def test_doctor_strict_check_accepts_available_browser(monkeypatch):
    monkeypatch.setattr(browser, "available_browsers", lambda: ["chrome"])
    monkeypatch.setattr(browser, "detect_default_browser", lambda: "chrome")
    result = invoke("doctor", "--check")
    assert result.exit_code == 0, result.exception


@pytest.mark.parametrize("name", ["inspect", "fill", "run", "batches", "generation"])
def test_retired_commands_are_not_public(name):
    result = invoke(name)
    assert result.exit_code == 2 and "No such command" in result.output


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_generation_commands_require_form_url(name):
    result = invoke(name)
    assert result.exit_code == 2 and "Missing argument" in result.output


def test_generate_defaults_to_one_response_and_submission(commands):
    result = invoke("generate", commands.config.url)
    assert result.exit_code == 0, result.exception
    assert commands.generated[0][0].count == 1
    assert len(commands.filled) == 1 and commands.filled[0][1]["submit"] is True
    assert commands.filled[0][1]["answers"] == [{1: "A", 2: "Generated 1"}]
    assert commands.filled[0][1].get("compact") is True
    assert commands.inspected[0][1].get("display") is False


def test_generate_passes_message_reference_count_and_browser(commands, tmp_path):
    reference = generator.write_csv(commands.config, profiles(), tmp_path / "reference.csv")
    result = invoke(
        "generate",
        commands.config.url,
        "-n",
        "3",
        "-m",
        "University students",
        "--like",
        str(reference),
        "--browser",
        "firefox",
    )
    assert result.exit_code == 0, result.exception
    config, rows, message, options = commands.generated[0]
    assert config.count == 3 and rows == profiles() and message == "University students"
    assert options["browser"] == "firefox"
    assert commands.filled[0][0].browser == "firefox"
    assert commands.filled[0][1]["submit"] is True


def test_generate_halt_keeps_submission_manual(commands):
    result = invoke("generate", commands.config.url, "--halt")
    assert result.exit_code == 0, result.exception
    assert commands.filled[0][1]["submit"] is False
    assert commands.filled[0][1].get("halt") is True


def test_prepare_saves_exact_json_without_filling(commands, tmp_path):
    result = invoke("prepare", commands.config.url, "-n", "2", "--message", "Short natural answers")
    assert result.exit_code == 0, result.exception
    assert not commands.filled
    plans = list(tmp_path.glob("gformbot-responses*.json"))
    assert len(plans) == 1
    config, answers = cli._load_plan(plans[0])
    assert config.count == 2 and answers == [{1: "A", 2: "Generated 1"}, {1: "B", 2: "Generated 2"}]
    assert commands.generated[0][2] == "Short natural answers"
    assert not list(tmp_path.glob("*.csv"))
    assert commands.inspected[0][1].get("display") is False
    assert json.loads(plans[0].read_text())["cleanup_on_success"] is False


@pytest.mark.parametrize("name", ["generate", "prepare"])
@pytest.mark.parametrize("source", ["--data", "--like"])
def test_public_csv_date_order(commands, tmp_path, monkeypatch, name, source):
    commands.config.questions = [QuestionConfig(1, "Date", "date", True)]
    csv = tmp_path / "dates.csv"
    csv.write_text("Date\n05/06/2028\n")
    before = csv.read_bytes()
    monkeypatch.setattr(cli, "_generate", lambda config, reference, *a, **kw: reference)
    result = invoke(name, commands.config.url, source, str(csv), "--date-order", "dmy")
    assert result.exit_code == 0, result.exception
    assert csv.read_bytes() == before
    config, rows = cli._load_plan(next(tmp_path.glob("gformbot-responses*.json")))
    assert rows == [{1: "2028-06-05"}]
    assert config.csv_date_order == "dmy"


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_explicit_date_order_overrides_matching_saved_setting(commands, tmp_path, name):
    commands.config.questions = [QuestionConfig(1, "Date", "date", True)]
    commands.config.csv_date_order = "mdy"
    save_config(commands.config, tmp_path / "gformbot_config.yaml")
    csv = tmp_path / "dates.csv"
    csv.write_text("Date\n05/06/2028\n")
    result = invoke(name, commands.config.url, "--data", str(csv), "--date-order", "dmy")
    assert result.exit_code == 0, result.exception
    _, rows = cli._load_plan(next(tmp_path.glob("gformbot-responses*.json")))
    assert rows == [{1: "2028-06-05"}]


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_ambiguous_csv_uses_saved_order_or_fails_without_guessing(commands, tmp_path, name):
    commands.config.questions = [QuestionConfig(1, "Date", "date", True)]
    csv = tmp_path / "dates.csv"
    csv.write_text("Date\n05/06/2028\n")
    result = invoke(name, commands.config.url, "--data", str(csv))
    assert result.exit_code != 0 and "ambiguous" in str(result.exception)
    assert not list(tmp_path.glob("gformbot-responses*.json"))
    commands.config.csv_date_order = "dmy"
    save_config(commands.config, tmp_path / "gformbot_config.yaml")
    result = invoke(name, commands.config.url, "--data", str(csv))
    assert result.exit_code == 0, result.exception
    _, rows = cli._load_plan(next(tmp_path.glob("gformbot-responses*.json")))
    assert rows == [{1: "2028-06-05"}]


def test_prepare_output_survives_confirmed_wizard_execution(commands, tmp_path, monkeypatch):
    assert invoke("prepare", commands.config.url).exit_code == 0
    path = next(tmp_path.glob("gformbot-responses*.json"))
    config, rows = cli._load_plan(path)
    identity = batch_id(json.loads(path.read_text()))
    monkeypatch.setattr(cli, "_next_action", lambda **kw: "submit")
    monkeypatch.setattr(cli, "_choose_browser", lambda: "headless")
    monkeypatch.setattr(cli, "_confirm_submit", lambda *a: None)

    def confirmed(config, **kwargs):
        config._record_submission(1)
        return True

    monkeypatch.setattr(cli, "_run_fill", confirmed)
    cli._manage_batch(config, rows, path, saved=True)
    payload = json.loads(path.read_text())
    assert payload["submitted_indices"] == [1]
    assert payload["batch_id"] == identity and payload["cleanup_on_success"] is False
    assert cli._load_plan(path)[0]._submitted_indices == [1]


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_default_count_uses_matching_saved_config(commands, tmp_path, name):
    config = copy.deepcopy(commands.config)
    config.count = 4
    save_config(config, tmp_path / "gformbot_config.yaml")
    result = invoke(name, config.url)
    assert result.exit_code == 0, result.exception
    assert commands.generated[0][0].count == 4


def test_different_form_does_not_inherit_saved_count(commands, tmp_path):
    config = copy.deepcopy(commands.config)
    config.count = 4
    save_config(config, tmp_path / "gformbot_config.yaml")
    result = invoke("prepare", config.url.replace("fixture", "other"))
    assert result.exit_code == 0, result.exception
    assert commands.generated[0][0].count == 1


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_csv_data_uses_ordered_complete_profiles_without_ai(commands, tmp_path, name):
    path = generator.write_csv(commands.config, profiles(), tmp_path / "my data.csv")
    original = path.read_bytes()
    result = invoke(name, commands.config.url, "--data", str(path), "-n", "2")
    assert result.exit_code == 0, result.exception
    assert not commands.generated and path.read_bytes() == original
    if name == "generate":
        assert commands.filled[0][1]["answers"] == profiles()
    else:
        plan = next(tmp_path.glob("gformbot-responses*.json"))
        assert cli._load_plan(plan)[1] == profiles()


def test_csv_data_never_repeats_rows_to_meet_excess_count(commands, tmp_path):
    path = generator.write_csv(commands.config, profiles(), tmp_path / "data.csv")
    result = invoke("generate", commands.config.url, "--data", str(path), "-n", "3")
    assert result.exit_code != 0 and not commands.generated and not commands.filled


@pytest.mark.parametrize("name", ["generate", "prepare"])
@pytest.mark.parametrize("submitted", [[], [1]])
def test_json_data_preserves_exact_pending_profiles_without_ai(commands, tmp_path, name, submitted):
    path = saved_plan(commands, tmp_path, submitted=submitted)
    pending = [row for index, row in enumerate(profiles(), 1) if index not in submitted]
    result = invoke(name, commands.config.url, "--data", str(path))
    assert result.exit_code == 0, result.exception
    assert not commands.generated
    if name == "generate":
        assert commands.filled[0][0].count == len(pending)
        assert commands.filled[0][1]["answers"] == pending
    else:
        restored, rows = cli._load_plan(path)
        remaining = [row for index, row in enumerate(rows, 1) if index not in restored._submitted_indices]
        assert remaining == pending
        assert not commands.filled


@pytest.mark.parametrize("outcome", ["attempted", "reviewing", "uncertain"])
def test_uncertain_saved_submission_stops_without_menu_even_in_a_terminal(
    commands, tmp_path, monkeypatch, outcome
):
    path = saved_plan(commands, tmp_path)
    original = path.read_bytes()
    payload = json.loads(original)
    journal = Journal(batch_id(payload))
    journal.set(1, outcome)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    result = invoke("generate", commands.config.url, "--data", str(path))
    assert result.exit_code != 0
    assert "uncertain" in str(result.exception).lower()
    assert not commands.generated and not commands.filled
    assert path.read_bytes() == original and journal.states()[1] == outcome


def test_json_data_rejects_count_override_that_would_change_pending_rows(commands, tmp_path):
    path = saved_plan(commands, tmp_path, submitted=[1])
    result = invoke("generate", commands.config.url, "--data", str(path), "-n", "2")
    assert result.exit_code != 0 and not commands.generated and not commands.filled


def test_completed_json_data_cannot_be_submitted_again(commands, tmp_path):
    path = saved_plan(commands, tmp_path, submitted=[1, 2])
    result = invoke("generate", commands.config.url, "--data", str(path))
    assert result.exit_code != 0 and not commands.generated and not commands.filled


def test_json_data_for_different_form_is_rejected(commands, tmp_path):
    path = saved_plan(commands, tmp_path)
    result = invoke("generate", commands.config.url.replace("fixture", "other"), "--data", str(path))
    assert result.exit_code != 0 and not commands.generated and not commands.filled


@pytest.mark.parametrize("name", ["generate", "prepare"])
@pytest.mark.parametrize("extra", [["--like"], ["-m", "Regenerate these answers"]])
def test_data_rejects_ai_options_before_inspection(commands, tmp_path, name, extra):
    path = generator.write_csv(commands.config, profiles(), tmp_path / "data.csv")
    args = extra + ([str(path)] if extra == ["--like"] else [])
    result = invoke(name, commands.config.url, "--data", str(path), *args)
    assert result.exit_code != 0
    assert not commands.inspected and not commands.generated and not commands.filled


@pytest.mark.parametrize("count", ["0", "501", "-1", "abc"])
def test_invalid_count_is_rejected_before_work(commands, count):
    result = invoke("generate", commands.config.url, "-n", count)
    assert result.exit_code == 2 and not commands.inspected


@pytest.mark.parametrize(
    "flags",
    [
        ["--submit"],
        ["--yes"],
        ["--output", "answers.csv"],
        ["--reference", "reference.csv"],
        ["--instructions", "answer naturally"],
        ["--checkpoint", "checkpoint.json"],
        ["--headless"],
    ],
)
def test_legacy_generation_flags_are_not_public(commands, flags):
    result = invoke("generate", commands.config.url, *flags)
    assert result.exit_code == 2 and not commands.inspected


def test_prepare_does_not_offer_halt(commands):
    result = invoke("prepare", commands.config.url, "--halt")
    assert result.exit_code == 2 and not commands.inspected


@pytest.mark.parametrize("name", ["generate", "prepare"])
def test_reference_requires_csv_file(commands, tmp_path, name):
    path = saved_plan(commands, tmp_path)
    result = invoke(name, commands.config.url, "--like", str(path))
    assert result.exit_code != 0 and not commands.generated and not commands.filled


def test_data_rejects_unrecognized_file_type(commands, tmp_path):
    path = tmp_path / "answers.txt"
    path.write_text("unrecognized input", encoding="utf-8")
    result = invoke("generate", commands.config.url, "--data", str(path))
    assert result.exit_code != 0 and not commands.generated and not commands.filled


def test_plan_save_failure_stops_before_filling(commands, monkeypatch):
    monkeypatch.setattr(cli, "_save_plan", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    result = invoke("generate", commands.config.url)
    assert result.exit_code != 0 and not commands.filled


def test_preview_explicit_plan_opens_default_app_without_execution(commands, tmp_path):
    path = saved_plan(commands, tmp_path)
    result = invoke("preview", str(path))
    assert result.exit_code == 0, result.exception
    assert [p.resolve() for p in commands.opened] == [path.resolve()]
    assert not commands.inspected and not commands.generated and not commands.filled


def test_preview_without_saved_files_returns_without_menu(commands):
    result = invoke("preview")
    assert result.exit_code == 0, result.exception
    assert not commands.opened and not commands.inspected and not commands.filled


def test_preview_lists_unfinished_runs_and_ai_jobs_newest_first(commands, tmp_path, monkeypatch):
    old = saved_plan(commands, tmp_path)
    payload = json.loads(old.read_text())
    payload["created_at"] = "2026-10-20T10:00:00+00:00"
    old.write_text(json.dumps(payload), encoding="utf-8")
    done = tmp_path / "completed.json"
    completed = {**payload, "submitted_indices": [1, 2]}
    done.write_text(json.dumps(completed), encoding="utf-8")
    job = tmp_path / "gformbot-generation-new.json"
    job.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "gformbot-generation",
                "context_date": "2026-10-22",
                "responses": [{"1": "A", "2": "Already generated"}, None],
                "request": {"config": config_to_dict(commands.config), "count": 2},
            }
        ),
        encoding="utf-8",
    )
    old_time = datetime.fromisoformat(payload["created_at"]).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(job, (old_time + 172_800, old_time + 172_800))
    seen = []

    def select(message, choices, **kwargs):
        seen.extend(choices)
        selected = next(choice for choice in choices if job.name in choice.name)
        return selected.value

    monkeypatch.setattr(cli, "_ask_select", select)
    result = invoke("preview")
    assert result.exit_code == 0, result.exception
    labels = [choice.name for choice in seen]
    assert next(i for i, name in enumerate(labels) if job.name in name) < next(
        i for i, name in enumerate(labels) if old.name in name
    )
    assert all(done.name not in name for name in labels)
    assert [p.resolve() for p in commands.opened] == [job.resolve()]
    assert not commands.inspected and not commands.generated and not commands.filled


@pytest.mark.parametrize("source", ["--data", "--like"])
def test_new_csv_does_not_inherit_another_files_column_mapping(commands, tmp_path, source):
    previous = copy.deepcopy(commands.config)
    previous.pool_file = str(tmp_path / "old.csv")
    previous.pool_column_map = {1: "Old choice column", 2: "Old written column"}
    previous.questions[0].pool_column = "Old choice column"
    save_config(previous, tmp_path / "gformbot_config.yaml")
    path = generator.write_csv(commands.config, profiles(), tmp_path / "new.csv")
    result = invoke("prepare", commands.config.url, source, str(path))
    assert result.exit_code == 0, result.exception
    if source == "--data":
        assert not commands.generated
        plan = next(tmp_path.glob("gformbot-responses*.json"))
        assert cli._load_plan(plan)[1] == profiles()[:1]
    else:
        assert commands.generated[0][1] == profiles()


def test_saved_defaults_keep_freshly_read_form_instructions(commands, tmp_path):
    previous = copy.deepcopy(commands.config)
    previous.instructions = [
        {"page": 1, "before_question": 1, "title": "Old instructions", "description": ""}
    ]
    save_config(previous, tmp_path / "gformbot_config.yaml")
    commands.config.instructions = [
        {"page": 1, "before_question": 1, "title": "New instructions", "description": ""}
    ]
    result = invoke("prepare", commands.config.url)
    assert result.exit_code == 0, result.exception
    assert commands.generated[0][0].instructions == commands.config.instructions


def test_json_input_with_changed_instructions_stops_before_execution(commands, tmp_path):
    path = saved_plan(commands, tmp_path)
    commands.config.instructions = [
        {"page": 1, "before_question": 1, "title": "Changed instructions", "description": ""}
    ]
    result = invoke("generate", commands.config.url, "--data", str(path))
    assert result.exit_code != 0 and "instructions have changed" in str(result.exception)
    assert not commands.filled and not commands.generated
