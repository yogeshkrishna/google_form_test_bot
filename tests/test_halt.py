import json
import os
from types import SimpleNamespace

import pytest

from gformbot import browser, cli, filler, inspector, logger
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig
from gformbot.execution import Runner
from gformbot.selenium_runtime import NoSuchWindowException, StaleElementReferenceException
from gformbot.state import Session


class PrivateTabs:
    def __init__(self):
        self.current_url = "https://docs.google.com/forms/d/e/test/viewform"
        self.window_handles = ["tab1"]
        self.current_window_handle = "tab1"
        self.switch_to = self
        self.confirmed = set()
        self.events = []
        self.stopped = False

    def window(self, handle):
        assert handle in self.window_handles
        self.current_window_handle = handle
        self.events.append(("switch", handle))

    def new_window(self, kind):
        assert kind == "tab"
        handle = f"tab{len(self.window_handles) + 1}"
        self.window_handles.append(handle)
        self.window(handle)

    def quit(self):
        assert self.stopped, "Halt must leave the forms open until the user stops."
        self.events.append(("quit",))


@pytest.fixture
def halted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=2,
        browser="auto",
        background=True,
        min_delay=0,
        max_delay=0,
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
    )
    answers = [{1: "First"}, {1: "Second"}]
    plan = cli._save_plan(config, answers)
    session = Session(plan, json.loads(plan.read_text()), digest(plan))
    driver = PrivateTabs()
    messages, sections, launches, fills, logs = [], [], [], [], []

    def no_prompt(*args, **kwargs):
        pytest.fail("Halt must work without menus or confirmation prompts.")

    ui = SimpleNamespace(
        _info=messages.append,
        _section=sections.append,
        _ask_select=no_prompt,
        _ask_text=no_prompt,
        _ask_confirm=no_prompt,
    )
    monkeypatch.setattr("gformbot.execution.sys.stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: config)
    monkeypatch.setattr("gformbot.execution.config_from_schema", lambda value: value)
    monkeypatch.setattr(browser, "get_driver", lambda name: launches.append(name) or driver)
    monkeypatch.setattr(logger, "log_result", lambda *args, **kwargs: logs.append((args, kwargs)))

    def fill(config, answer, **options):
        assert options["submit"] is False and "before_submit" not in options
        options["before_review"]()
        assert session.journal.states()[len(fills) + 1] == "reviewing"
        fills.append((driver.current_window_handle, answer, options))
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", fill)
    monkeypatch.setattr(
        filler,
        "_positive_confirmation",
        lambda driver, *args: driver.current_window_handle in driver.confirmed,
    )
    return SimpleNamespace(
        runner=Runner(config, answers, ui, session),
        config=config,
        session=session,
        plan=plan,
        driver=driver,
        launches=launches,
        fills=fills,
        logs=logs,
        messages=messages,
        sections=sections,
    )


def test_halt_fills_private_tabs_without_prompts_or_tool_submission(halted, monkeypatch):
    h = halted

    def close(seconds):
        assert seconds == 1
        assert [handle for handle, _, _ in h.fills] == ["tab1", "tab2"]
        assert h.driver.current_window_handle == "tab2"
        assert h.session.journal.states() == {1: "reviewing", 2: "reviewing"}
        assert ("quit",) not in h.driver.events
        h.driver.stopped = True
        h.driver.window_handles = []

    monkeypatch.setattr("gformbot.execution.time.sleep", close)
    assert h.runner.run(halt=True, submit=True, compact=True) is False
    assert h.launches == ["auto"]
    assert not h.sections
    assert h.session.journal.states() == {1: "uncertain", 2: "uncertain"}
    assert h.plan.exists()


def test_halt_records_manual_confirmation_before_user_closes_other_tabs(halted, monkeypatch):
    h = halted
    scans = []

    def user(seconds):
        scans.append(True)
        if len(scans) == 1:
            h.driver.confirmed.add("tab1")
        else:
            assert h.session.journal.states() == {1: "confirmed", 2: "reviewing"}
            assert json.loads(h.plan.read_text())["submitted_indices"] == [1]
            h.driver.stopped = True
            h.driver.window_handles = []

    monkeypatch.setattr("gformbot.execution.time.sleep", user)
    assert h.runner.run(halt=True) is False
    assert h.session.journal.states() == {1: "confirmed", 2: "uncertain"}
    assert [args[1] for args, _ in h.logs if args[4] == "submitted manually"] == [1]
    assert h.plan.exists()


def test_halt_keeps_windows_open_even_after_every_manual_submission_is_confirmed(halted, monkeypatch):
    h = halted
    scans = []

    def user(seconds):
        scans.append(True)
        if len(scans) == 1:
            h.driver.confirmed.update(h.driver.window_handles)
        else:
            assert h.session.journal.states() == {1: "confirmed", 2: "confirmed"}
            assert ("quit",) not in h.driver.events
            h.driver.stopped = True
            h.driver.window_handles = []

    monkeypatch.setattr("gformbot.execution.time.sleep", user)
    assert h.runner.run(halt=True) is True
    assert len(scans) == 2


def test_halt_ctrl_c_stops_without_a_confirmation_menu(halted, monkeypatch):
    h = halted

    def stop(seconds):
        h.driver.stopped = True
        raise KeyboardInterrupt()

    monkeypatch.setattr("gformbot.execution.time.sleep", stop)
    assert h.runner.run(halt=True) is False
    assert h.session.uncertain() == [1, 2]
    assert h.plan.exists()


def test_halt_dry_run_opens_nothing(halted, monkeypatch):
    h = halted
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: pytest.fail("Dry run must remain offline"))
    assert h.runner.run(halt=True, dry_run=True, compact=True) is False
    assert not h.launches and not h.fills
    assert h.session.journal.states() == {}


def test_halt_rejects_invalid_answers_before_browser_launch(halted):
    h = halted
    h.runner.answers[0][1] = ""
    with pytest.raises(ValueError, match="required answer is blank"):
        h.runner.run(halt=True)
    assert not h.launches


def test_halt_tab_closing_during_confirmation_scan_does_not_close_other_tabs(halted, monkeypatch):
    h = halted
    switch = h.driver.window

    def close_first(handle):
        if handle == "tab1" and "tab1" in h.driver.window_handles:
            h.driver.window_handles.remove("tab1")
            raise NoSuchWindowException("The user closed this tab.")
        switch(handle)

    monkeypatch.setattr(h.driver, "window", close_first)

    def close_rest(seconds):
        assert h.driver.window_handles == ["tab2"]
        assert h.session.journal.states() == {1: "uncertain", 2: "reviewing"}
        assert ("quit",) not in h.driver.events
        h.driver.stopped = True
        h.driver.window_handles = []

    monkeypatch.setattr("gformbot.execution.time.sleep", close_rest)
    assert h.runner.run(halt=True) is False


def test_halt_tolerates_confirmation_document_changing_during_manual_submission(halted, monkeypatch):
    h = halted
    changes = []

    def navigating(*args):
        if not changes:
            changes.append(True)
            raise StaleElementReferenceException("The manual submission replaced this document.")
        return False

    monkeypatch.setattr(filler, "_positive_confirmation", navigating)

    def stop(seconds):
        assert ("quit",) not in h.driver.events
        h.driver.stopped = True
        raise KeyboardInterrupt()

    monkeypatch.setattr("gformbot.execution.time.sleep", stop)
    assert h.runner.run(halt=True) is False
    assert changes == [True]


def test_halt_large_count_needs_no_tab_count_confirmation(halted, monkeypatch):
    h = halted
    h.config.count = 21
    h.runner.answers = [{1: f"Response {index}"} for index in range(21)]
    h.runner.session = None
    filled = []
    monkeypatch.setattr(
        filler, "submit_response", lambda c, answer, **kw: filled.append(answer) or (True, "filled")
    )

    def close(seconds):
        assert len(h.driver.window_handles) == len(filled) == 21
        h.driver.stopped = True
        h.driver.window_handles = []

    monkeypatch.setattr("gformbot.execution.time.sleep", close)
    assert h.runner.run(halt=True, compact=True) is False
    assert not h.sections


@pytest.mark.browser
@pytest.mark.skipif(
    os.environ.get("GFORMBOT_BROWSER_TESTS") != "1",
    reason="Set GFORMBOT_BROWSER_TESTS=1 to run local Chrome integration tests.",
)
def test_halt_local_chrome_keeps_filled_tabs_open_without_submission(tmp_path, monkeypatch, html_page):
    monkeypatch.chdir(tmp_path)
    html = """<html><body><script>window.submits=0</script>
    <form onsubmit="window.submits++;event.preventDefault()">
    <div role="listitem" data-item-id="42"><div role="heading">Answer</div>
    <input type="text" name="entry.21" required></div>
    <button type="submit">Submit</button></form></body></html>"""
    config = RunConfig(
        html_page(html),
        count=2,
        browser="auto",
        min_delay=0,
        max_delay=0,
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="21", item_id="42")],
    )
    answers = [{1: "Literal first response"}, {1: "Second response stays separate"}]
    plan = cli._save_plan(config, answers)
    session = Session(plan, json.loads(plan.read_text()), digest(plan))
    launched = []
    real_factory = browser.get_driver

    def launch(name):
        assert name == "auto"
        driver = real_factory("chrome")
        launched.append(driver)
        return driver

    monkeypatch.setattr(browser, "get_driver", launch)
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: config)
    monkeypatch.setattr("gformbot.execution.config_from_schema", lambda value: value)

    def no_prompt(*args, **kwargs):
        pytest.fail("Halt must not ask for input.")

    ui = SimpleNamespace(
        _info=lambda message: None,
        _section=no_prompt,
        _ask_select=no_prompt,
        _ask_confirm=no_prompt,
        _ask_text=no_prompt,
    )
    checks = []

    def close_forms(seconds):
        driver = launched[0]
        handles = driver.window_handles
        assert len(handles) == 2
        for handle, answer in zip(handles, answers, strict=True):
            driver.switch_to.window(handle)
            assert driver.find_element("name", "entry.21").get_attribute("value") == answer[1]
            assert driver.execute_script("return window.submits") == 0
        checks.append(True)
        for handle in handles:
            driver.switch_to.window(handle)
            driver.close()  # Simulated user closure; no Submit is clicked.

    monkeypatch.setattr("gformbot.execution.time.sleep", close_forms)
    assert Runner(config, answers, ui, session).run(halt=True, compact=True) is False
    assert checks == [True] and len(launched) == 1
    assert session.journal.states() == {1: "uncertain", 2: "uncertain"}
    assert plan.exists()
