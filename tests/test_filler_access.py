"""Access changes must not become successful or silently retried responses."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote

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


def test_verified_email_requires_manual_acknowledgement(monkeypatch):
    consent = Control("Record student@example.test as the email to be included with my response")
    box = Box([consent])
    config = RunConfig("url")
    config.email_collection = "verified"
    monkeypatch.setattr(filler, "_question_boxes", lambda d: [box])
    monkeypatch.setattr(filler, "_check_interstitial", lambda d: None)
    with pytest.raises(ValueError, match="check.*email"):
        filler._verify_email_consent(object(), config)
    assert not consent.checked
    filler._verify_email_consent(object(), config, lambda: setattr(consent, "checked", True))
    assert filler._page_questions(object(), config) == []


def test_ordinary_checkbox_is_not_mistaken_for_account_consent(monkeypatch):
    consent = Control("Record student@example.test as the email to be included with my response", True)
    box = Box([consent], names=["entry.5"])
    assert filler._verified_email_control(box) is None


@pytest.mark.parametrize("association", ["aria-labelledby", "label"])
def test_verified_consent_uses_only_its_associated_label(association):
    class Label(Control):
        text = "Record student@example.test as the email to be included with my response"

        def get_attribute(self, name):
            return "account" if name == "for" else None

        def find_elements(self, *args):
            return []

    class Checkbox(Control):
        def get_attribute(self, name):
            if name == "aria-labelledby":
                return "consent" if association == "aria-labelledby" else None
            return "account" if name == "id" else super().get_attribute(name)

    checkbox, label = Checkbox(), Label()

    class LabelledBox:
        def find_elements(self, by, selector):
            if selector == '[name^="entry."]':
                return []
            if selector == "[role='checkbox'], input[type='checkbox']":
                return [checkbox]
            if selector == ("consent" if association == "aria-labelledby" else "label"):
                return [label]
            return []

    box = LabelledBox()
    assert filler._verified_email_control(box) is checkbox
    label.get_attribute = lambda name: "unrelated" if name == "for" else None
    checkbox.get_attribute = lambda name: None
    assert filler._verified_email_control(box) is None


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


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Local Chrome tests are opt-in.")
@pytest.mark.parametrize("policy", ["input", "verified", "verified-label", "verified-labelledby"])
def test_collected_email_and_account_consent_in_local_browser(policy):
    from gformbot.browser import get_driver

    email = '<input type="email" name="emailAddress">'
    label = "Record student@example.test as the email to be included with my response"
    if policy.startswith("verified"):
        attrs = f'aria-label="{label}"'
        extra = ""
        if policy == "verified-label":
            attrs, extra = 'id="consent"', f'<label for="consent">{label}</label>'
        elif policy == "verified-labelledby":
            attrs, extra = 'aria-labelledby="consent"', f'<span id="consent">{label}</span>'
        email = f'<input type="checkbox" name="emailAddress" {attrs}>{extra}'
    document = f"""<html><body><div role="listitem"><div role="heading">Email</div>{email}</div>
    <div role="listitem"><div role="heading">Answer</div><input name="entry.42"></div>
    <button>Submit</button></body></html>"""
    questions = [QuestionConfig(1, "Answer", "text", entry_id="42")]
    answers = {1: "Exact answer"}
    if policy == "input":
        questions.insert(0, QuestionConfig(2, "Email", "text", entry_id="emailAddress"))
        answers[2] = "student@example.test"
    config = RunConfig("data:text/html;charset=utf-8," + quote(document), questions=questions)
    config.email_collection = "verified" if policy.startswith("verified") else policy
    driver = get_driver("chrome", headless=True)
    consent_calls = []

    def human_checks_consent():
        consent_calls.append(True)
        driver.find_element("name", "emailAddress").click()

    try:
        result = filler.execute_response(driver, config, answers, confirm_email=human_checks_consent)
        assert result["status"] == "filled"
        assert driver.find_element("name", "entry.42").get_attribute("value") == "Exact answer"
        field = driver.find_element("name", "emailAddress")
        if policy.startswith("verified"):
            assert field.is_selected() and consent_calls == [True]
        else:
            assert field.get_attribute("value") == "student@example.test" and not consent_calls
    finally:
        driver.quit()
