"""Account restrictions reject cleanly; exposed responses retain uncertain state."""

import json
from types import SimpleNamespace

import pytest

from gformbot import browser, cli, filler, inspector
from gformbot.access import FormAccessError
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig
from gformbot.execution import Runner
from gformbot.state import Session

URL = "https://docs.google.com/forms/d/e/session_fixture/viewform"


class SessionDriver:
    def __init__(self):
        self.quits = 0
        self.current_window_handle = "first"
        self.window_handles = ["first"]
        self.current_url = URL
        self.page_source = "<form><input name='entry.1'></form>"
        self.switch_to = self

    def window(self, handle):
        assert handle in self.window_handles
        self.current_window_handle = handle

    def execute_script(self, *args):
        pass

    def quit(self):
        self.quits += 1


def form_config(**options):
    return RunConfig(
        URL,
        form_title="Session fixture",
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="101")],
        sections=["Section 1"],
        min_delay=0,
        max_delay=0,
        **options,
    )


@pytest.fixture
def reviewed_batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = form_config(count=2)
    answers = [{1: "First fictional answer"}, {1: "Second fictional answer"}]
    path = cli._save_plan(config, answers, tmp_path / "answers.json")
    session = Session(path, json.loads(path.read_text()), digest(path))
    messages = []
    ui = SimpleNamespace(_info=messages.append, _choice=lambda *args: args)
    return SimpleNamespace(
        runner=Runner(config, answers, ui, session),
        config=config,
        session=session,
        driver=SessionDriver(),
        path=path,
        ui=ui,
        messages=messages,
    )


@pytest.mark.parametrize(
    "document",
    [
        "<h1>Sign in to continue</h1>",
        "<h1>You need permission</h1>",
        "<h1>You have already responded</h1>",
        "<h1>This form is no longer accepting responses</h1>",
    ],
)
def test_manual_access_block_cannot_be_mistaken_for_confirmation(reviewed_batch, monkeypatch, document):
    batch = reviewed_batch
    batch.session.review(2)
    batch.driver.current_url = URL.replace("viewform", "formResponse")
    batch.driver.page_source = document
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: pytest.fail("Blocked confirmation"))
    assert batch.runner.confirmed(batch.driver, 2) is None
    assert batch.session.journal.states() == {2: "uncertain"}
    assert batch.session.confirmed == set()
    assert batch.path.exists() and json.loads(batch.path.read_text()).get("submitted_indices", []) == []
    assert any("Response 2 stopped" in message for message in batch.messages)


@pytest.mark.parametrize("tabs", [False, True])
def test_manual_wait_stops_on_expired_access_without_confirming(reviewed_batch, monkeypatch, tabs):
    batch = reviewed_batch
    batch.session.review(1)
    batch.driver.page_source = "<h1>Sign in to continue</h1>"
    batch.ui._ask_text = lambda *a: None
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: pytest.fail("Blocked confirmation"))
    pending = {"first": 1}
    assert batch.runner.wait_manual(batch.driver, pending, tabs=tabs) is False
    assert pending == {} and batch.session.journal.states() == {1: "uncertain"}
    assert batch.driver.quits == 0


def test_halt_keeps_blocked_browser_open_until_user_stops(reviewed_batch, monkeypatch):
    batch = reviewed_batch
    batch.session.review(1)
    batch.driver.page_source = "<h1>You have already responded</h1>"
    pending = {"first": 1}
    checks = []

    def stop(seconds):
        assert seconds == 1 and batch.driver.window_handles == ["first"]
        assert batch.driver.quits == 0 and pending == {}
        assert batch.session.journal.states() == {1: "uncertain"}
        checks.append(True)
        raise KeyboardInterrupt()

    monkeypatch.setattr("gformbot.execution.time.sleep", stop)
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: pytest.fail("Blocked confirmation"))
    assert batch.runner.wait_halt(batch.driver, pending) is False
    assert checks == [True] and batch.driver.quits == 0


def test_background_access_failure_closes_browser_and_keeps_pending_answers(reviewed_batch, monkeypatch):
    batch = reviewed_batch
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: batch.driver)
    choices = iter(["open", 1, "check"])
    batch.ui._ask_select = lambda *a, **kw: next(choices)

    def fill(config, answer, **options):
        options["before_review"]()
        batch.driver.page_source = "<h1>This form is no longer accepting responses</h1>"
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", fill)
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: pytest.fail("Blocked confirmation"))
    pending = {1: 1, 2: 2}
    assert batch.runner.background(pending) is False
    assert pending == {1: 1, 2: 2} and batch.driver.quits == 1
    assert batch.session.journal.states() == {1: "uncertain"}
    assert batch.path.exists()


@pytest.mark.parametrize("restriction", ["requires_signin", "one_response", "email_collection"])
@pytest.mark.parametrize("changed", [False, True])
def test_runner_rejects_account_forms_before_browser_or_submission(
    tmp_path, monkeypatch, restriction, changed
):
    monkeypatch.chdir(tmp_path)
    config = form_config()
    current = form_config()
    setattr(
        current if changed else config, restriction, "verified" if restriction == "email_collection" else True
    )
    monkeypatch.setattr(inspector, "inspect_form", lambda url: current)
    monkeypatch.setattr("gformbot.execution.config_from_schema", lambda schema: schema)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("Account form opened a browser"))
    monkeypatch.setattr(filler, "submit_response", lambda *a, **kw: pytest.fail("Account form filled"))
    monkeypatch.setattr("gformbot.execution.sys.stdin", SimpleNamespace(isatty=lambda: True))
    ui = SimpleNamespace(_info=lambda message: None)
    with pytest.raises(FormAccessError, match="without an account"):
        Runner(config, [{1: "Fictional answer"}], ui).run(halt=True)
