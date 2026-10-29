"""Public-only scope and predictable process diagnostics."""

import json

import pytest
from click.testing import CliRunner

from gformbot import browser, cli, inspector
from gformbot.config import QuestionConfig, RunConfig, config_to_dict


@pytest.mark.parametrize("command", ["generate", "prepare"])
@pytest.mark.parametrize("flag", ["--sign-in", "--allow-uploads"])
def test_retired_account_and_file_flags_fail_before_network(command, flag, monkeypatch):
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: pytest.fail("No form access expected"))
    result = CliRunner().invoke(cli.cli, [command, "https://example.invalid", flag])
    assert result.exit_code == 2
    assert "No such option" in result.output


@pytest.mark.parametrize(
    "restriction", [{"requires_signin": True}, {"one_response": True}, {"email_collection": "verified"}]
)
def test_inspection_rejects_account_bound_forms_without_opening_browser(restriction, monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[QuestionConfig(1, "Answer", "text", True)],
        **restriction,
    )
    monkeypatch.setattr(inspector, "inspect_form", lambda *a, **kw: config)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("No sign-in browser"))
    with pytest.raises(ValueError, match="Only public forms"):
        cli._run_inspect(config.url, display=False)


def test_runtime_diagnostics_use_stderr_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cli", lambda **kw: (_ for _ in ()).throw(ValueError("Invalid profile")))
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: Invalid profile\n"


def test_debug_mode_preserves_exception_for_diagnostics(monkeypatch):
    monkeypatch.setenv("GFORMBOT_DEBUG", "1")
    monkeypatch.setattr(cli, "cli", lambda **kw: (_ for _ in ()).throw(RuntimeError("Broken fixture")))
    with pytest.raises(RuntimeError, match="Broken fixture"):
        cli.main()


def test_missing_config_points_to_available_command(tmp_path):
    result = CliRunner().invoke(cli.cli, ["config", str(tmp_path / "missing.yaml"), "--show"])
    assert "gformbot prepare URL" in str(result.exception)
    assert "gformbot inspect" not in str(result.exception)
    assert "prepare" in cli.cli.commands


def test_doctor_strict_check_fails_without_browser(monkeypatch):
    monkeypatch.setattr(browser, "available_browsers", lambda: [])
    result = CliRunner().invoke(cli.cli, ["doctor", "--check"])
    assert result.exit_code == 1
    assert "Setup incomplete" in result.output
    assert "supported browser" in result.output
    assert CliRunner().invoke(cli.cli, ["doctor"]).exit_code == 0


def test_execution_ui_exposes_only_its_explicit_contract():
    ui = cli._run_ui()
    assert callable(ui._info) and callable(ui._ask_select)
    assert not hasattr(ui, "os") and not hasattr(ui, "load_config")


def test_cancel_is_reported_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cli", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 130
    captured = capsys.readouterr()
    assert captured.out == "" and "Cancelled" in captured.err


def test_legacy_account_checkpoint_cannot_resume_generation(tmp_path, monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        requires_signin=True,
        questions=[QuestionConfig(1, "Answer", "text", True)],
    )
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "kind": "gformbot-generation",
                "schema_version": 1,
                "request": {"config": config_to_dict(config), "count": 2},
                "responses": [{"1": "Accepted earlier"}, None],
            }
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()
    monkeypatch.setattr(cli, "_generation_browser", lambda: pytest.fail("No account-bound generation"))
    with pytest.raises(ValueError, match="Only public forms"):
        cli._resume_generation(path, resume=True)
    assert path.read_bytes() == original
    cli._resume_generation(path, export=tmp_path / "accepted.csv")
    assert "Accepted earlier" in (tmp_path / "accepted.csv").read_text(encoding="utf-8")
    assert path.read_bytes() == original
