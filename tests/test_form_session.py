import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import click
import pytest
from click.testing import CliRunner

from gformbot import browser, cli, filler, inspector, logger
from gformbot.access import FormAccessError
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig, config_from_schema
from gformbot.execution import Runner
from gformbot.state import Session, UncertainSubmission

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


def form_document():
    item = [101, "Answer", None, 0, [[1, None, 1]]]
    metadata = ["", [item], ["Your response has been recorded."]] + [None] * 5 + ["Session fixture"]
    data = [None, metadata] + [None] * 17
    data[18] = 1  # Fictional sign-in-required form; no account identifiers.
    return (
        "<html><body><script>var FB_PUBLIC_LOAD_DATA_ = "
        + json.dumps(data)
        + ";</script><form action='/formResponse' method='get'>"
        "<div role='listitem' data-item-id='101'><div role='heading'>Answer</div>"
        "<input name='entry.1' required></div><button type='submit'>Submit</button></form></body></html>"
    )


def test_form_browser_reuses_visible_session_after_user_sign_in(monkeypatch):
    driver, launches, inspected, prompts = SessionDriver(), [], [], []
    monkeypatch.setattr(
        browser, "get_driver", lambda name, headless: launches.append((name, headless)) or driver
    )
    config = form_config(requires_signin=True)

    def inspect(url, **options):
        inspected.append((url, options["driver"]))
        if len(inspected) == 1:
            raise FormAccessError("login_required")
        return config

    monkeypatch.setattr(inspector, "inspect_form", inspect)
    resource = browser.FormBrowser("headless", prompts.append)
    try:
        assert resource.inspect(URL) is config
        assert launches == [("auto", False)]
        assert inspected == [(URL, driver), (URL, driver)]
        assert len(prompts) == 1 and "Do not submit" in prompts[0]
        assert driver.quits == 0
    finally:
        resource.close()
    resource.close()
    assert driver.quits == 1 and resource.driver is None


@pytest.mark.parametrize("requires_signin", [True, False])
def test_available_schema_requests_sign_in_once_only_when_required(monkeypatch, requires_signin):
    driver, launches, inspected, prompts = SessionDriver(), [], [], []
    config = form_config(requires_signin=requires_signin)
    monkeypatch.setattr(
        browser, "get_driver", lambda name, headless: launches.append((name, headless)) or driver
    )

    def inspect(url, **options):
        inspected.append((url, options["driver"]))
        return config

    monkeypatch.setattr(inspector, "inspect_form", inspect)
    resource = browser.FormBrowser("chrome", prompts.append)
    try:
        assert resource.inspect(URL) is config
        assert resource.inspect(URL) is config
        assert launches == [("chrome", False)]
        assert inspected == [(URL, driver)] * (3 if requires_signin else 2)
        assert len(prompts) == (1 if requires_signin else 0)
        assert driver.quits == 0
    finally:
        resource.close()
    assert driver.quits == 1


@pytest.mark.parametrize("state", ["permission_denied", "already_answered", "closed", "temporary_failure"])
def test_form_browser_does_not_retry_non_login_blocks(monkeypatch, state):
    driver, prompts, calls = SessionDriver(), [], []
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: driver)

    def inspect(*args, **kwargs):
        calls.append(True)
        raise FormAccessError(state)

    monkeypatch.setattr(inspector, "inspect_form", inspect)
    resource = browser.FormBrowser("chrome", prompts.append)
    try:
        with pytest.raises(FormAccessError) as caught:
            resource.inspect(URL)
        assert caught.value.state == state
        assert calls == [True] and prompts == []
    finally:
        resource.close()


def test_cancelled_sign_in_and_cleanup_failure_preserve_original_error(monkeypatch):
    class BrokenCleanup(SessionDriver):
        def quit(self):
            super().quit()
            raise RuntimeError("cleanup failed")

    driver = BrokenCleanup()
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: driver)

    def blocked(*args, **kwargs):
        raise FormAccessError("login_required")

    def cancel(message):
        raise KeyboardInterrupt()

    monkeypatch.setattr(inspector, "inspect_form", blocked)
    resource = browser.FormBrowser("chrome", cancel)
    with pytest.raises(KeyboardInterrupt):
        try:
            resource.inspect(URL)
        finally:
            resource.close()
    assert driver.quits == 1 and resource.driver is None


def test_verified_email_confirmation_is_left_to_the_user(monkeypatch):
    prompts, driver = [], SessionDriver()
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: driver)
    resource = browser.FormBrowser("chrome", prompts.append)
    try:
        resource.confirm_email()
        assert len(prompts) == 1
        assert "if you agree" in prompts[0] and "Do not submit" in prompts[0]
    finally:
        resource.close()


def test_click_context_owns_one_browser_and_closes_once(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    drivers = []

    def launch(*args, **kwargs):
        driver = SessionDriver()
        drivers.append(driver)
        return driver

    monkeypatch.setattr(browser, "get_driver", launch)
    with click.Context(cli.cli):
        first = cli._open_form_browser("chrome")
        assert cli._open_form_browser("edge") is first
        assert cli._form_browser() is first
        assert drivers[0].quits == 0
    assert len(drivers) == 1 and drivers[0].quits == 1


def test_noninteractive_sign_in_does_not_open_a_browser(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("Unexpected browser"))
    with click.Context(cli.cli), pytest.raises(ValueError, match="interactive terminal"):
        cli._open_form_browser("chrome")


def test_explicit_sign_in_inspection_keeps_resource_on_config(monkeypatch):
    schema = inspector.parse_form_html(form_document(), URL)
    inspected = []
    resource = SimpleNamespace(inspect=lambda url: inspected.append(url) or schema)
    monkeypatch.setattr(cli, "_open_form_browser", lambda name: resource)
    monkeypatch.setattr(inspector, "inspect_form", lambda *a, **kw: pytest.fail("Anonymous inspection"))
    config = cli._run_inspect(URL, browser="edge", display=False, sign_in=True)
    assert config._form_browser is resource
    assert inspected == [URL] and config.requires_signin


def test_quiet_inspection_never_opens_a_login_window(monkeypatch):
    schema = inspector.parse_form_html(form_document(), URL)
    monkeypatch.setattr(inspector, "inspect_form", lambda *a, **kw: schema)
    monkeypatch.setattr(cli, "_open_form_browser", lambda *a: pytest.fail("Unexpected login window"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    with pytest.raises(FormAccessError) as caught:
        cli._run_inspect(URL, display=False)
    assert caught.value.state == "login_required"


def test_prepare_sign_in_option_reaches_shared_preparation(tmp_path, monkeypatch):
    calls = []
    path = tmp_path / "answers.json"

    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return form_config(), [{1: "Answer"}], path, True

    monkeypatch.setattr(cli, "_command_answers", prepare)
    result = CliRunner().invoke(cli.cli, ["prepare", URL, "--sign-in", "--browser", "edge"])
    assert result.exit_code == 0, result.output
    assert calls == [((URL, None, "", None, None, "edge", True, None), {"cleanup_on_success": False})]


def test_one_response_limit_stops_before_generation_or_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = form_config(requires_signin=True, one_response=True)
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **kw: config)
    monkeypatch.setattr(cli, "_generate", lambda *a, **kw: pytest.fail("Unexpected generation"))
    monkeypatch.setattr(cli, "_save_plan", lambda *a, **kw: pytest.fail("Unexpected saved batch"))
    with pytest.raises(ValueError, match="one response per Google account"):
        cli._command_answers(URL, 2, "", None, None, "chrome", True)
    assert list(tmp_path.iterdir()) == []


def test_saved_answers_do_not_serialize_the_authenticated_browser(tmp_path):
    config = form_config(requires_signin=True, one_response=True, email_collection="verified")
    config._form_browser = SimpleNamespace(driver=object(), authentication="fictional-session-marker")
    path = cli._save_plan(config, [{1: "A fictional answer"}], tmp_path / "answers.json")
    text = path.read_text(encoding="utf-8")
    assert "fictional-session-marker" not in text and "_form_browser" not in text
    restored, answers = cli._load_plan(path)
    assert answers == [{1: "A fictional answer"}]
    assert restored.requires_signin and restored.one_response and restored.email_collection == "verified"
    assert not hasattr(restored, "_form_browser")


def test_runner_uses_borrowed_authenticated_driver_and_leaves_cleanup_to_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = form_config(requires_signin=True, email_collection="verified", background=True)
    driver, inspected, fills = SessionDriver(), [], []

    def confirm_email():
        pass

    resource = SimpleNamespace(
        driver=driver,
        inspect=lambda url: inspected.append(url) or config,
        confirm_email=confirm_email,
    )
    config._form_browser = resource
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("Second browser"))
    monkeypatch.setattr(inspector, "inspect_form", lambda *a, **kw: pytest.fail("Anonymous inspection"))
    monkeypatch.setattr(logger, "log_result", lambda *a, **kw: None)

    def fill(current, answer, **options):
        fills.append((current, answer, options))
        return True, "submitted"

    monkeypatch.setattr(filler, "submit_response", fill)
    ui = SimpleNamespace(config_from_schema=lambda schema: schema, _info=lambda message: None)
    assert Runner(config, [{1: "A fictional answer"}], ui).run(submit=True, compact=True)
    assert inspected == [URL]
    assert callable(fills[0][2].pop("observe"))
    assert fills[0][2] == {"submit": True, "driver": driver, "confirm_email": confirm_email}
    assert driver.quits == 0 and not config.headless and not config.background


def test_runner_rechecks_newly_enabled_one_response_limit_before_filling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = form_config(count=2)
    current = form_config(count=2, one_response=True)
    monkeypatch.setattr(inspector, "inspect_form", lambda *a, **kw: current)
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: pytest.fail("Unexpected browser"))
    ui = SimpleNamespace(config_from_schema=lambda schema: schema)
    with pytest.raises(ValueError, match="one response per Google account"):
        Runner(config, [{1: "First"}, {1: "Second"}], ui).run(submit=True)


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


def test_background_check_keeps_borrowed_browser_and_all_pending_answers(reviewed_batch, monkeypatch):
    batch = reviewed_batch
    batch.config._form_browser = SimpleNamespace(driver=batch.driver, confirm_email=lambda: None)
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
    assert pending == {1: 1, 2: 2} and batch.driver.quits == 0
    assert batch.session.journal.states() == {1: "uncertain"}
    assert batch.path.exists()


@pytest.mark.parametrize("outcome", ["confirmed", "cancel", "failure"])
def test_automatic_verified_consent_tracks_manual_exposure_and_own_submit(tmp_path, monkeypatch, outcome):
    monkeypatch.chdir(tmp_path)
    config = form_config(requires_signin=True, email_collection="verified")
    answers = [{1: "A fictional answer"}]
    path = cli._save_plan(config, answers, tmp_path / "answers.json")
    session = Session(path, json.loads(path.read_text()), digest(path))
    other = Session(path, json.loads(path.read_text()), digest(path))
    driver, events = SessionDriver(), []

    def consent():
        assert session.journal.states() == {1: "reviewing"}
        events.append("consent")
        with pytest.raises(UncertainSubmission, match="already reviewing"):
            other.before_submit(1)
        if outcome == "cancel":
            raise KeyboardInterrupt()
        if outcome == "failure":
            raise FormAccessError("login_required")

    config._form_browser = SimpleNamespace(driver=driver, inspect=lambda url: config, confirm_email=consent)

    def fill(current, answer, **options):
        options["before_review"]()
        options["confirm_email"]()
        options["before_submit"]()
        assert session.journal.states() == {1: "attempted"}
        events.append("submit")
        return True, "submitted"

    monkeypatch.setattr(filler, "submit_response", fill)
    monkeypatch.setattr(logger, "log_result", lambda *a, **kw: None)
    ui = SimpleNamespace(config_from_schema=lambda schema: schema, _info=lambda message: None)
    runner = Runner(config, answers, ui, session)
    if outcome == "confirmed":
        assert runner.run(submit=True, compact=True)
        assert events == ["consent", "submit"]
        assert session.journal.states() == {1: "confirmed"}
        assert json.loads(path.read_text())["submitted_indices"] == [1]
    else:
        error = KeyboardInterrupt if outcome == "cancel" else ValueError
        with pytest.raises(error):
            runner.run(submit=True, compact=True)
        assert events == ["consent"] and session.journal.states() == {1: "reviewing"}
        assert json.loads(path.read_text()).get("submitted_indices", []) == []
    assert driver.quits == 0 and path.exists()


@pytest.mark.browser
@pytest.mark.skipif(
    os.environ.get("GFORMBOT_BROWSER_TESTS") != "1",
    reason="Set GFORMBOT_BROWSER_TESTS=1 for local Chrome integration.",
)
@pytest.mark.parametrize("after_submit", ["confirmed", "login_required", "closed"])
def test_local_sign_in_session_survives_inspection_and_submission(tmp_path, monkeypatch, after_submit):
    monkeypatch.chdir(tmp_path)
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path)
            if path.path == "/login":
                self.send_response(302)
                self.send_header("Set-Cookie", "fixture_session=ready; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/viewform")
                self.end_headers()
                return
            signed_in = "fixture_session=ready" in self.headers.get("Cookie", "")
            if not signed_in:
                content = "<h1>Sign in to continue</h1><a href='/login'>Sign in</a>"
            elif path.path == "/formResponse":
                accepted.append(parse_qs(path.query)["entry.1"][0])
                content = {
                    "confirmed": "<div class='vHW8K'>Your response has been recorded.</div>",
                    "login_required": "<h1>Sign in to continue</h1>",
                    "closed": "<h1>This form is no longer accepting responses.</h1>",
                }[after_submit]
            else:
                content = form_document()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/viewform"
    real_clean_url, real_factory = inspector.clean_url, browser.get_driver
    launches, prompts = [], []

    def fixture_url(value, **options):
        return (
            value
            if value.startswith(f"http://127.0.0.1:{server.server_port}/")
            else real_clean_url(value, **options)
        )

    def launch(name, headless):
        assert (name, headless) == ("chrome", False)
        launches.append(True)
        return real_factory("chrome", headless=True)

    resource = None

    def sign_in(message):
        prompts.append(message)
        resource.driver.find_element("link text", "Sign in").click()  # Local fixture only.

    monkeypatch.setattr(inspector, "clean_url", fixture_url)
    monkeypatch.setattr(browser, "get_driver", launch)
    try:
        resource = browser.FormBrowser("chrome", sign_in)
        config = config_from_schema(resource.inspect(url))
        config._form_browser = resource
        config.min_delay = config.max_delay = 0
        ui = SimpleNamespace(config_from_schema=config_from_schema, _info=lambda message: None)
        runner = Runner(config, [{1: "A local fictional answer"}], ui)
        if after_submit == "confirmed":
            assert runner.run(submit=True, compact=True)
        else:
            with pytest.raises(ValueError, match="uncertain"):
                runner.run(submit=True, compact=True)
        assert accepted == ["A local fictional answer"] and len(prompts) == len(launches) == 1
        assert resource.driver.current_url.startswith(f"http://127.0.0.1:{server.server_port}/formResponse")
    finally:
        if resource is not None:
            resource.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
