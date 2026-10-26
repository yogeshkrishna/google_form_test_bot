import io
import sqlite3
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

from gformbot import cli, filler
from gformbot.followups import Recorder, safe_url
from gformbot.state import Journal


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://chat.whatsapp.com/InviteCode", "https://chat.whatsapp.com/InviteCode"),
        ("https://discord.gg/abc", "https://discord.gg/abc"),
        ("https://t.me/example?start=invite", "https://t.me/example?start=invite"),
        ("https://forms.gle/abc", "https://forms.gle/abc"),
        ("/next", "https://example.test/next"),
        ("mailto:coordinator@example.test", "mailto:coordinator@example.test"),
        ("tel:+123456789", "tel:+123456789"),
        ("https://www.google.com/url?q=https%3A%2F%2Fdiscord.gg%2Fabc&usg=discard", "https://discord.gg/abc"),
        ("https://evil.test/url?q=https://discord.gg/abc", "https://evil.test/url?q=https://discord.gg/abc"),
        ("javascript:alert(1)", ""),
        ("data:text/html,hello", ""),
        ("https://name:password@example.test/", ""),
        ("https://accounts.google.com/AccountChooser", ""),
        ("https://docs.google.com/forms/d/e/form/viewform?edit2=secret", ""),
    ],
)
def test_link_destinations(url, expected):
    assert safe_url(url, "https://example.test/viewform") == expected


def test_secret_parameters_are_redacted_without_destroying_public_path():
    result = safe_url("https://example.test/join?token=private&group=public#auth=private", "")
    assert "private" not in result
    assert "group=public" in result and "REDACTED" in result


def test_existing_journal_upgrade_and_retry_deduplication(tmp_path):
    journal = Journal("batch")
    journal.path.parent.mkdir(parents=True)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "CREATE TABLE responses(batch TEXT,response INTEGER,state TEXT,PRIMARY KEY(batch,response))"
        )
        connection.execute("INSERT INTO responses VALUES('batch',7,'confirmed')")
    blocks = [{"text": "Join us", "links": ["https://discord.gg/abc"]}]
    for _ in range(2):
        journal.record_followups(7, 2, "section", blocks, str(tmp_path / "runs.csv"), "Form")
    journal.record_followups(9, 2, "section", blocks, str(tmp_path / "runs.csv"), "Form")
    assert journal.states() == {7: "confirmed"}
    assert [r["response"] for r in journal.followups()] == [7, 9]
    assert Journal("other").followups(log_path=str(tmp_path / "other.csv")) == []


class Page:
    def __init__(self, blocks):
        self.blocks = blocks
        self.current_url = "https://example.test/viewform"

    def execute_script(self, script, *args):
        return self.blocks


def recorder(tmp_path):
    return Recorder(Journal("batch"), str(tmp_path / "runs.csv"), "Form")


def test_summary_is_short_full_log_keeps_every_description(tmp_path):
    notes = recorder(tmp_path)
    notes.observe(Page([{"text": "Long question instructions", "links": []}]), 7, 1, "section")
    notes.observe(Page([{"text": "click here", "links": ["https://discord.gg/abc"]}]), 7, 1, "section")
    notes.observe(Page([{"text": "Your response has been recorded.", "links": []}]), 7, 0, "confirmation")
    messages = []
    notes.show(SimpleNamespace(_info=messages.append))
    output = "\n".join(messages)
    assert "https://discord.gg/abc" in output
    assert "Long question instructions" not in output
    assert "Your response has been recorded" not in output
    assert f'log "{tmp_path / "runs.csv"}" --followups' in output
    result = CliRunner().invoke(cli.cli, ["log", str(tmp_path / "runs.csv"), "--followups"])
    assert result.exit_code == 0
    assert "Long question instructions" in result.output and "Response 7" in result.output


def test_unicode_display_and_storage_failure_do_not_escape(tmp_path, monkeypatch):
    notes = recorder(tmp_path)
    monkeypatch.setattr(
        notes.journal, "record_followups", lambda *args: (_ for _ in ()).throw(OSError("secret"))
    )
    notes.observe(Page([{"text": "வணக்கம் नमस्ते", "links": []}]), 3, 0, "confirmation")
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    console = Console(file=stream, force_terminal=False)
    notes.show(SimpleNamespace(console=console, _info=console.print))
    stream.flush()
    data = stream.buffer.getvalue().decode("cp1252")
    assert "secret" not in data and "\\u" in data
    assert "could not" in data.lower()


def test_manual_confirmation_only_collects_after_live_positive_check(tmp_path, monkeypatch):
    from gformbot.execution import Runner

    config = SimpleNamespace(
        url="https://example.test/viewform",
        confirmation_text="",
        log_file=str(tmp_path / "runs.csv"),
        form_title="Form",
    )
    runner = Runner(config, [{}], SimpleNamespace(_info=lambda text: None))
    page = Page([{"text": "Email the coordinator", "links": []}])
    monkeypatch.setattr("gformbot.access.check_browser_access", lambda *args: None)
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: False)
    assert runner.confirmed(page, 1) is False
    assert runner.followups.journal.followups() == []
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: True)
    assert runner.confirmed(page, 1) is True
    assert runner.followups.journal.followups()[0]["stage"] == "confirmation"


def test_failed_display_does_not_escape(tmp_path):
    notes = recorder(tmp_path)
    notes.observe(Page([{"text": "Next step", "links": []}]), 1, 0, "confirmation")
    notes.show(SimpleNamespace(_info=lambda text: (_ for _ in ()).throw(OSError("closed stream"))))


def test_manual_reconciliation_does_not_invent_a_confirmation(tmp_path, monkeypatch):
    from gformbot.execution import Runner

    config = SimpleNamespace(log_file=str(tmp_path / "runs.csv"), form_title="Form")
    session = SimpleNamespace(identity="batch", ids=[17], digest="digest", confirm=lambda index: None)
    runner = Runner(config, [{}], SimpleNamespace(), session)
    monkeypatch.setattr(runner, "log", lambda *args: None)
    runner.confirm(1)
    assert runner.followups.journal.followups() == []


def test_log_limits_responses_and_keeps_custom_logs_separate(tmp_path):
    journal = Journal("batch")
    block = [{"text": "Instructions", "links": []}]
    first, second = str(tmp_path / "first.csv"), str(tmp_path / "second.csv")
    journal.record_followups(7, 1, "section", block, first, "First")
    journal.record_followups(7, 2, "section", block, second, "Second")
    journal.record_followups(9, 1, "section", block, first, "First")
    assert [r["response"] for r in journal.followups(log_path=first, last=1)] == [9]
    assert [r["section"] for r in journal.followups(log_path=second)] == [2]


def test_text_urls_are_sanitized_without_logging_raw_errors(tmp_path):
    notes = recorder(tmp_path)
    page = Page(
        [
            {
                "text": "Visit https://name:PRIVATE@example.test or https://example.test/?token=PRIVATE",
                "links": [],
            }
        ]
    )
    notes.observe(page, 1, 0, "confirmation")
    records = notes.journal.followups()
    assert "PRIVATE" not in str(records)
    assert "REDACTED" in str(records)
    messages = []
    notes.show(SimpleNamespace(_info=messages.append))
    assert "may not work" in messages[0]


def test_confirmation_navigation_is_not_a_followup_link():
    base = "https://docs.google.com/forms/d/e/form/formResponse"
    assert safe_url("./viewform", base) == ""
    assert safe_url("#", base) == ""
    assert safe_url("", base) == ""
    assert safe_url("https://docs.google.com/forms/d/e/other/viewform", base).endswith("/other/viewform")


def test_plain_text_links_also_appear_in_the_short_summary(tmp_path):
    notes = recorder(tmp_path)
    notes.observe(Page([{"text": "Join https://discord.gg/public.", "links": []}]), 1, 1, "section")
    records = notes.journal.followups()
    assert records[0]["content"]["links"] == ["https://discord.gg/public"]
    messages = []
    notes.show(SimpleNamespace(_info=messages.append))
    assert "Join https://discord.gg/public." in messages[0]
