from contextlib import contextmanager
from pathlib import Path
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from gformbot import filler as browser
from gformbot.config import RunConfig, QuestionConfig

GFormBotError = RuntimeError


class Control:
    def __init__(self, label, checked=False):
        self.label = label
        self.checked = checked
        self.clicks = 0
        self.text = label

    def is_displayed(self):
        return True

    def get_attribute(self, name):
        return {"data-value": self.label, "aria-label": self.label,
                "aria-checked": str(self.checked).lower()}.get(name)

    def is_selected(self):
        return self.checked

    def click(self):
        self.clicks += 1
        self.checked = not self.checked


class Box:
    def __init__(self, controls):
        self.controls = controls

    def find_elements(self, by, selector):
        return self.controls


class Driver:
    def execute_script(self, *args):
        pass


def question(index="1", kind="checkbox", title="Where?", section=0, required=True, options=None):
    return QuestionConfig(int(index), title, kind, required, options or ["A", "B"], section + 1, entry_id=str(index), item_id=str(index))


def test_checkbox_duplicates_do_not_toggle_a_checked_choice_off():
    selected = Control("A", True)
    unwanted = Control("B", True)
    q = question()
    browser._fill_question(Driver(), q, Box([selected, unwanted]), ["A", "A"], 0.1)
    assert selected.checked and selected.clicks == 0
    assert not unwanted.checked and unwanted.clicks == 1


def test_comma_containing_option_matches_exactly():
    selected = Control("Roads, footpaths or crossings")
    other = Control("Bus stops")
    browser._fill_question(Driver(), question(), Box([selected, other]), [selected.label], 0.1)
    assert selected.checked
    assert other.clicks == 0


def test_missing_option_is_an_error_before_any_click():
    control = Control("A")
    with pytest.raises(GFormBotError, match="no longer has options"):
        browser._fill_question(Driver(), question(), Box([control]), ["Different"], 0.1)
    assert control.clicks == 0


def test_ignored_first_click_is_retried_without_toggling_selected_checkbox(monkeypatch):
    selected = Control("A", True)
    class DelayedControl(Control):
        def click(self):
            self.clicks += 1
            if self.clicks > 1:
                self.checked = True
    delayed = DelayedControl("B")
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))
    def wait(driver, timeout, condition, message):
        assert condition(driver) is False
        assert condition(driver) is True
    monkeypatch.setattr(browser, "_wait", wait)
    browser._fill_question(Driver(), question(), Box([selected, delayed]), ["A", "B"], 1)
    assert selected.clicks == 0 and delayed.clicks == 2


def test_selection_retry_is_bounded_when_a_control_never_retains(monkeypatch):
    class IgnoredControl(Control):
        def click(self):
            self.clicks += 1
    ignored = IgnoredControl("A")
    clock = iter(range(1, 10))
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))
    def wait(driver, timeout, condition, message):
        for _ in range(5):
            assert condition(driver) is False
        raise RuntimeError(message)
    monkeypatch.setattr(browser, "_wait", wait)
    with pytest.raises(RuntimeError, match="bounded selection"):
        browser._fill_question(Driver(), question(kind="radio"), Box([ignored]), "A", 1)
    assert ignored.clicks == 3


def test_confirmation_requires_endpoint_and_content(monkeypatch):
    class ConfirmationDriver:
        current_url = "https://docs.google.com/forms/d/e/FORM/viewform"

        def find_elements(self, by, selector):
            class Text:
                text = "Your response has been recorded."
                def is_displayed(self):
                    return True
            return [Text()]

    driver = ConfirmationDriver()
    monkeypatch.setattr(browser, "_validation_errors", lambda d: [])
    monkeypatch.setattr(browser, "_question_boxes", lambda d: [])
    assert not browser._positive_confirmation(driver)
    driver.current_url = "https://accounts.google.com/signin"
    assert not browser._positive_confirmation(driver)
    driver.current_url = "https://docs.google.com/forms/d/e/FORM/formResponse"
    assert browser._positive_confirmation(driver)
    assert browser._positive_confirmation(driver, "Your response has been recorded.")
    assert not browser._positive_confirmation(driver, "Different custom confirmation")
    driver.current_url = "https://unrelated.example/formResponse"
    assert not browser._positive_confirmation(driver, expected_url="https://docs.google.com/forms/d/e/FORM/viewform")
    driver.current_url = "https://docs.google.com/forms/d/e/FORM/formResponse"
    monkeypatch.setattr(browser, "_validation_errors", lambda d: ["Required"])
    assert not browser._positive_confirmation(driver)


def test_info_only_section_requires_inspected_heading_and_no_expected_questions():
    class Heading:
        text = "Read this before continuing"
        def is_displayed(self):
            return True

    class HeadingDriver:
        def find_elements(self, *args):
            return [Heading()]

    form = RunConfig("http://localhost/viewform", "Test", questions=[question(section=1)],
                     sections=["Read this before continuing", "Answers"])
    assert browser._empty_section_marker(HeadingDriver(), form, 0) is not None
    assert browser._empty_section_marker(HeadingDriver(), form, 1) is None
    form.sections[0] = "Another page"
    assert browser._empty_section_marker(HeadingDriver(), form, 0) is None


def test_submit_without_confirmation_is_uncertain_and_clicked_only_once(monkeypatch):
    clicks = []

    class TestDriver:
        current_url = "https://docs.google.com/forms/d/e/FORM/viewform"
        def get(self, url):
            pass
        def find_elements(self, *args):
            return [object()]

    q = question(kind="radio")
    config = RunConfig(TestDriver.current_url, questions=[q], sections=["One"])
    monkeypatch.setattr(browser, "_check_interstitial", lambda d: None)
    monkeypatch.setattr(browser, "_page_questions", lambda d, f: [(q, object())])
    monkeypatch.setattr(browser, "_fill_question", lambda *args: None)
    monkeypatch.setattr(browser, "_validation_errors", lambda d: [])
    monkeypatch.setattr(browser, "_button", lambda d, label: object())
    monkeypatch.setattr(browser, "_click", lambda *args: clicks.append("Submit"))

    def wait(driver, timeout, condition, message):
        if "acceptance" in message:
            raise RuntimeError(message)
        return True

    monkeypatch.setattr(browser, "_wait", wait)
    with pytest.raises(browser.SubmissionUncertainError, match="acceptance"):
        browser.execute_response(TestDriver(), config, {1: "A"}, submit=True)
    assert clicks == ["Submit"]


@pytest.fixture
def local_form():
    content = (Path(__file__).parent / "fixtures" / "multipage_form.html").read_bytes()
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path.endswith("/formResponse"):
                accepted.append(json.loads(parse_qs(urlparse(self.path).query)["answers"][0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/viewform"
    questions = [
        question("1", "radio", "Repeated title", 0),
        question("2", "checkbox", "Where?", 0, options=["Roads, footpaths or crossings", "Bus stops", "Other"]),
        question("3", "radio", "Repeated title", 1),
        question("4", "text", "Optional note", 1, required=False, options=[]),
        question("5", "dropdown", "How often?", 1, options=["Always", "Sometimes", "Never"]),
        question("6", "textarea", "Comment", 2, options=[]),
        question("7", "scale", "Comfort", 2, options=["1", "2", "3"]),
    ]
    form = RunConfig(url, "Local form fixture", questions=questions, sections=["One", "Two", "Three"])
    answers = {"entry.1": "A", "entry.2": ["Roads, footpaths or crossings", "Bus stops"],
               "entry.3": "B", "entry.4": "", "entry.5": "Sometimes", "entry.6": "A synthetic fixture answer.", "entry.7": "2"}
    yield form, {int(key.removeprefix("entry.")): value for key, value in answers.items()}, accepted
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Set GFORMBOT_BROWSER_TESTS=1 to run local Chrome integration tests.")
def test_local_multipage_fill_and_submit(local_form, tmp_path):
    form, answers, accepted = local_form
    from gformbot.browser import get_driver
    driver = get_driver(browser="chrome", headless=True)
    try:
        result = browser.execute_response(driver, form, answers, submit=False)
        assert result["status"] == "filled"
        assert len(result["filled_questions"]) == 7
        assert accepted == []
        result = browser.execute_response(driver, form, answers, submit=True)
        assert result["status"] == "confirmed"
    finally:
        driver.quit()
    assert accepted == [{str(key): value for key, value in answers.items()}]


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Set GFORMBOT_BROWSER_TESTS=1 to run local Chrome integration tests.")
def test_local_validation_blocks_next(local_form, tmp_path):
    form, answers, accepted = local_form
    answers[2] = ["Roads, footpaths or crossings", "Bus stops", "Other"]
    from gformbot.browser import get_driver
    driver = get_driver(browser="chrome", headless=True)
    try:
        with pytest.raises(GFormBotError, match="validation"):
            browser.execute_response(driver, form, answers, submit=True)
    finally:
        driver.quit()
    assert accepted == []


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Set GFORMBOT_BROWSER_TESTS=1 to run local Chrome integration tests.")
@pytest.mark.parametrize("mode", ["wait", "tabs"])
def test_review_batch_with_real_private_tabs_and_manual_confirmation(local_form, tmp_path, monkeypatch, mode):
    from gformbot import cli, browser as factory, inspector
    from gformbot.artifacts import digest
    form, answer, accepted = local_form
    form.browser, form.headless, form.count = "chrome", False, 2
    form.min_delay = form.max_delay = 0
    form.log_file = str(tmp_path / "runs.csv")
    answers = [dict(answer), {**answer, 6: "Second response, preserved separately."}]
    target = cli._save_plan(form, answers, tmp_path / "plan.json")
    cli._track_plan(form, answers, target, digest(target))
    driver = factory.get_driver("chrome", headless=True)
    monkeypatch.setattr(factory, "get_driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: form)
    monkeypatch.setattr(cli, "config_from_schema", lambda value: value)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_ask_select", lambda *args, **kwargs: mode)
    original_confirmation = browser._positive_confirmation
    before_next = []
    original_fill = browser.submit_response
    def fill(config, row, **kwargs):
        assert kwargs["submit"] is False
        before_next.append(len(accepted))
        return original_fill(config, row, **kwargs)
    monkeypatch.setattr(browser, "submit_response", fill)
    if mode == "wait":
        def confirm(d, *args):
            if not original_confirmation(d, *args):
                browser._button(d, "Submit").click()  # Local fixture simulates the human click.
            return original_confirmation(d, *args)
        monkeypatch.setattr(browser, "_positive_confirmation", confirm)
    else:
        checks = []
        def check(message):
            checks.append(message)
            assert len(driver.window_handles) == 2 and not accepted
            for index, handle in enumerate(driver.window_handles):
                driver.switch_to.window(handle)
                text = driver.find_element("css selector", "textarea").get_attribute("value")
                assert text == answers[index][6]
                browser._button(driver, "Submit").click()
            return ""
        monkeypatch.setattr(cli, "_ask_text", check)
    assert cli._run_fill(form, answers=answers) is True
    assert before_next == ([0, 1] if mode == "wait" else [0, 0])
    assert accepted == [{str(k): v for k, v in a.items()} for a in answers]
    assert json.loads(target.read_text())["submitted_indices"] == [1, 2]


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Set GFORMBOT_BROWSER_TESTS=1 to run local Chrome integration tests.")
def test_background_batch_opens_only_requested_profiles_in_review_browser(local_form, tmp_path, monkeypatch):
    from gformbot import cli, browser as factory, inspector
    from gformbot.artifacts import digest
    form, answer, accepted = local_form
    form.browser, form.headless, form.background, form.count = "chrome", False, True, 2
    form.min_delay = form.max_delay = 0
    form.log_file = str(tmp_path / "runs.csv")
    answers = [dict(answer), {**answer, 6: "Explicitly opened second response."}]
    target = cli._save_plan(form, answers, tmp_path / "plan.json")
    cli._track_plan(form, answers, target, digest(target))
    real_factory = factory.get_driver
    launches = []
    def launch(browser, headless):
        launches.append(headless)
        # Both test windows are headless; production receives the requested flag.
        return real_factory(browser, headless=True)
    monkeypatch.setattr(factory, "get_driver", launch)
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: form)
    monkeypatch.setattr(cli, "config_from_schema", lambda value: value)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    selections = iter(["tabs", "open", 1, "check", "open", 2])
    def select(message, choices, **kwargs):
        value = next(selections)
        if value == "open" and launches == [True]:
            assert not accepted
        return value
    monkeypatch.setattr(cli, "_ask_select", select)
    original_confirmation = browser._positive_confirmation
    def confirm(driver, *args):
        if not original_confirmation(driver, *args):
            browser._button(driver, "Submit").click()  # Simulated human, local fixture only.
        return original_confirmation(driver, *args)
    monkeypatch.setattr(browser, "_positive_confirmation", confirm)
    assert cli._run_fill(form, answers=answers) is True
    assert launches == [True, False]
    assert accepted == [{str(k): v for k, v in a.items()} for a in answers]
    assert json.loads(target.read_text())["submitted_indices"] == [1, 2]
