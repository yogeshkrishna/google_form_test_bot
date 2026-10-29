"""Access changes must not become successful or silently retried responses."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from gformbot import filler
from gformbot.config import QuestionConfig, RunConfig


class Control:
    text = ""

    def __init__(self, label="", checked=False, name=""):
        self.label, self.checked, self.name = label, checked, name

    def get_attribute(self, name):
        return {"aria-label": self.label, "aria-checked": str(self.checked).lower(), "name": self.name}.get(
            name
        )

    def is_displayed(self):
        return True

    def is_selected(self):
        return self.checked


class Box:
    def __init__(self, controls, names=()):
        self.controls, self.names = controls, names

    def find_elements(self, by, selector):
        if "name" in selector:
            return [Control(name=name) for name in self.names]
        return self.controls


@pytest.fixture
def response(monkeypatch):
    events = []
    driver = SimpleNamespace(current_url="https://docs.google.com/forms/d/e/FORM/viewform")
    driver.get = lambda url: events.append("get")
    driver.find_elements = lambda *args: [object()]
    question = QuestionConfig(1, "Answer", "text", entry_id="42")
    config = RunConfig(driver.current_url, questions=[question], sections=["One"])
    monkeypatch.setattr(filler, "_check_interstitial", lambda d: None)
    monkeypatch.setattr(filler, "_page_questions", lambda d, f: [(question, object())])
    monkeypatch.setattr(filler, "_fill_question", lambda *args: events.append("fill"))
    monkeypatch.setattr(filler, "_validation_errors", lambda d: [])
    monkeypatch.setattr(filler, "_button", lambda d, label: object())
    monkeypatch.setattr(filler, "_click", lambda *args: events.append("click"))
    monkeypatch.setattr(filler, "_wait", lambda d, t, condition, message: condition(d))
    return driver, config, events


def test_review_is_marked_after_navigation_and_before_answer_interaction(response):
    driver, config, events = response
    filler.execute_response(driver, config, {1: "A"}, before_review=lambda: events.append("review"))
    assert events == ["get", "review", "fill"]


@pytest.mark.parametrize("outcome", ["filled", "confirmed", "rejected", "uncertain"])
def test_observation_never_invents_success(response, monkeypatch, outcome):
    driver, config, events = response
    observed = []
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: outcome == "confirmed")
    monkeypatch.setattr(
        filler,
        "_validation_errors",
        lambda d: ["Rejected"] if outcome == "rejected" and "click" in events else [],
    )
    options = dict(submit=outcome != "filled", observe=lambda d, section, stage: observed.append(stage))
    if outcome in {"rejected", "uncertain"}:
        expected = (
            filler.SubmissionRejectedError if outcome == "rejected" else filler.SubmissionUncertainError
        )
        with pytest.raises(expected):
            filler.execute_response(driver, config, {1: "A"}, **options)
    else:
        assert filler.execute_response(driver, config, {1: "A"}, **options)["status"] == outcome
    assert observed == (["section", "confirmation"] if outcome == "confirmed" else ["section"])


def test_broken_observer_cannot_fail_a_confirmed_submission(response, monkeypatch, caplog):
    driver, config, events = response
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: True)

    def broken(*args):
        raise OSError("PRIVATE URL")

    assert (
        filler.execute_response(driver, config, {1: "A"}, submit=True, observe=broken)["status"]
        == "confirmed"
    )
    assert events.count("click") == 1
    assert "PRIVATE" not in caplog.text


def test_closure_during_capture_cannot_undo_the_confirmation(response, monkeypatch):
    driver, config, events = response
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: True)

    def closed(d, section, stage):
        if stage == "confirmation":
            del d.current_url
            raise OSError("Browser closed during observation")

    result = filler.execute_response(driver, config, {1: "A"}, submit=True, observe=closed)
    assert result["status"] == "confirmed" and events.count("click") == 1


def test_blocked_initial_page_never_enters_review(response, monkeypatch):
    driver, config, events = response

    def blocked(d):
        raise ValueError("Sign in required")

    monkeypatch.setattr(filler, "_check_interstitial", blocked)
    with pytest.raises(ValueError, match="Sign in"):
        filler.execute_response(driver, config, {1: "A"}, before_review=lambda: events.append("review"))
    assert events == ["get"]


def test_access_loss_after_submit_is_uncertain_not_a_confirmation(response, monkeypatch):
    from gformbot.access import FormAccessError

    driver, config, events = response

    def access(d):
        if "click" in events:
            raise FormAccessError("already_answered")

    monkeypatch.setattr(filler, "_check_interstitial", access)
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *args: True)
    with pytest.raises(filler.SubmissionUncertainError):
        filler.execute_response(
            driver, config, {1: "A"}, submit=True, before_submit=lambda: events.append("intent")
        )
    assert events == ["get", "fill", "intent", "click"]


def test_next_reports_access_expiry_instead_of_waiting_for_controls(response, monkeypatch):
    from gformbot.access import FormAccessError

    driver, config, events = response
    config.sections.append("Two")

    def access(d):
        if "click" in events:
            raise FormAccessError("login_required")

    monkeypatch.setattr(filler, "_check_interstitial", access)
    monkeypatch.setattr(filler, "_button", lambda d, label: None if label == "Submit" else object())
    with pytest.raises(FormAccessError) as error:
        filler.execute_response(driver, config, {1: "A"})
    assert error.value.state == "login_required"
    assert not driver._gformbot_submission_clicked


def test_collected_email_maps_to_its_special_control(monkeypatch):
    email = QuestionConfig(1, "Email", "text", entry_id="emailAddress", item_id="emailAddress")
    box = Box([], names=["emailAddress"])
    monkeypatch.setattr(filler, "_question_boxes", lambda d: [box])
    assert filler._page_questions(object(), RunConfig("url", questions=[email])) == [(email, box)]


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Local Chrome tests are opt-in.")
@pytest.mark.parametrize("stage", ["initial", "next", "submit"])
def test_access_changes_in_local_browser(stage):
    from gformbot.access import FormAccessError
    from gformbot.browser import get_driver

    blocked = "<html><body><h1>You have already responded</h1></body></html>"
    label = "Next" if stage == "next" else "Submit"
    question = """<div role="listitem"><div role="heading">Answer</div>
    <input name="entry.42"></div>"""
    document = (
        f"""<html><body>{question}<button onclick="location.href='/blocked'">{label}</button></body></html>"""
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                (blocked if stage == "initial" or self.path == "/blocked" else document).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    driver = None
    try:
        driver = get_driver("chrome", headless=True)
        config = RunConfig(
            f"http://127.0.0.1:{server.server_port}/viewform",
            questions=[QuestionConfig(1, "Answer", "text", entry_id="42")],
            sections=["One", "Two"] if stage == "next" else ["One"],
        )
        error_type = filler.SubmissionUncertainError if stage == "submit" else FormAccessError
        with pytest.raises(error_type, match="already answered"):
            filler.execute_response(driver, config, {1: "Exact answer"}, submit=True, timeout=2)
        assert driver._gformbot_submission_clicked is (stage == "submit")
    finally:
        if driver:
            driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("restriction", ["requires_signin", "one_response", "email_collection"])
def test_account_bound_forms_never_navigate_or_fill(response, restriction):
    from gformbot.access import FormAccessError

    driver, config, events = response
    setattr(config, restriction, "verified" if restriction == "email_collection" else True)
    with pytest.raises(FormAccessError, match="without an account"):
        filler.execute_response(driver, config, {1: "Fictional answer"}, submit=True)
    assert events == []


def test_restricted_submit_does_not_even_start_a_browser(monkeypatch):
    from gformbot import browser
    from gformbot.access import FormAccessError

    config = RunConfig("https://docs.google.com/forms/d/e/restricted/viewform", requires_signin=True)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("Restricted browser opened"))
    with pytest.raises(FormAccessError, match="without an account"):
        filler.submit_response(config, {})


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures")
def test_public_collected_email_in_local_browser(html_page):
    from gformbot.browser import get_driver

    page = html_page(
        '<div role="listitem"><div role="heading">Email</div><input type="email" name="emailAddress"></div><button>Submit</button>'
    )
    config = RunConfig(
        page,
        email_collection="input",
        questions=[QuestionConfig(1, "Email", "text", entry_id="emailAddress")],
    )
    driver = get_driver("chrome", headless=True)
    try:
        assert filler.execute_response(driver, config, {1: "fictional@example.test"})["status"] == "filled"
        assert driver.find_element("name", "emailAddress").get_attribute("value") == "fictional@example.test"
    finally:
        driver.quit()


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures")
def test_delayed_editable_field_waits_without_overriding_restrictions(html_page):
    from gformbot.browser import get_driver

    page = html_page(
        '<input id="answer" type="email" disabled readonly>'
        '<script>setTimeout(()=>{const e=document.getElementById("answer");'
        "e.disabled=false;e.readOnly=false;},500)</script>"
    )
    driver = get_driver("chrome", headless=True)
    try:
        driver.get(page)
        field = driver.find_element("id", "answer")
        filler._set_value(driver, field, "fictional@example.test", 3, "Email was not retained")
        assert field.get_attribute("value") == "fictional@example.test"
        driver.execute_script("arguments[0].readOnly=true;", field)
        with pytest.raises(RuntimeError, match="did not become editable"):
            filler._set_value(driver, field, "changed@example.test", 0.1, "Email was not retained")
        assert field.get_attribute("value") == "fictional@example.test"
    finally:
        driver.quit()


def test_driver_failure_has_actionable_message_and_preserves_diagnostic_cause(monkeypatch):
    from gformbot.selenium_runtime import TimeoutException

    original = TimeoutException("Timed out receiving message from renderer", stacktrace=["private stack"])
    monkeypatch.setattr(filler, "execute_response", lambda *a, **kw: (_ for _ in ()).throw(original))
    config = RunConfig("https://docs.google.com/forms/d/e/public/viewform")
    with pytest.raises(RuntimeError, match="resume the saved responses") as failure:
        filler.submit_response(config, {}, driver=object())
    assert failure.value.__cause__ is original
    assert "Stacktrace" not in str(failure.value) and "private stack" not in str(failure.value)
