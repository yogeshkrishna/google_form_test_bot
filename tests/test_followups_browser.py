import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from test_routing_browser import configuration, response

from gformbot import browser, execution, filler, inspector
from gformbot.artifacts import digest
from gformbot.cli import _save_plan
from gformbot.execution import Runner
from gformbot.followups import _COLLECT, Recorder
from gformbot.state import Journal, Session

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable local Chrome fixtures."
    ),
]


@pytest.fixture(scope="module")
def driver():
    value = browser.get_driver("chrome", headless=True)
    yield value
    value.quit()


@pytest.fixture
def form_server():
    content = (Path(__file__).parent / "fixtures" / "branching_form.html").read_text(encoding="utf-8")
    content = content.replace(
        "<form>",
        """<form>
      <div class="cBGGJ">Intro: <a href="https://www.google.com/url?q=https%3A%2F%2Fdiscord.gg%2Fpublic&amp;usg=discard">click here</a></div>
      <nav><div class="gubaDc">Account credentials</div></nav>
      <footer><div class="gubaDc">Google footer</div></footer>""",
    )
    content = content.replace(
        '<h2 class="F9yp7e">Study</h2>',
        """<h2 class="F9yp7e">Study</h2>
      <div class="PqKxpe">Study directions <a href="/students">student site</a></div>""",
    )
    content = content.replace(
        '<h2 class="F9yp7e">Work</h2>',
        """<h2 class="F9yp7e">Work</h2>
      <div class="PqKxpe">Work directions <a href="/workers">worker site</a></div>""",
    )
    content = content.replace(
        '<h2 role="heading">Comment</h2>',
        """<h2 role="heading">Comment</h2>
      <div class="gubaDc">Plain question instructions
      <span hidden>HIDDEN SECRET</span></div>""",
    )
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path == "/formResponse":
                accepted.append(json.loads(parse_qs(urlparse(self.path).query)["answers"][0]))
                result = """<html><meta charset="utf-8"><body>
                  <div class="vHW8K">Thanks! Email the coordinator.<br>வணக்கம்
                    <a href="https://chat.whatsapp.com/PublicInvite">Join here</a>
                    <a href="https://example.test/join?token=PRIVATE&amp;group=public">Member link</a>
                  </div>
                  <a href="https://docs.google.com/forms/d/e/form/viewform?edit2=PRIVATE">Edit response</a>
                  <a href="/viewform">Submit another response</a>
                  <footer>Google footer</footer></body></html>"""
            else:
                result = content
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(result.encode("utf-8"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/viewform", accepted
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize(
    "mode,fault",
    [
        ("auto", ""),
        ("fill", ""),
        ("wait", ""),
        ("tabs", ""),
        ("background", ""),
        ("halt", ""),
        ("auto", "capture"),
        ("auto", "storage"),
        ("auto", "display"),
        ("resume", ""),
        ("batch", ""),
    ],
)
def test_directions_survive_every_run_mode(driver, form_server, tmp_path, monkeypatch, mode, fault):
    url, accepted = form_server
    config = configuration(url)
    config.confirmation_text = (
        ""  # Trust the existing live confirmation marker, not a fixture-specific sentence.
    )
    config.count = 2 if mode in {"resume", "batch"} else 1
    config.browser, config.headless = "chrome", mode == "fill"
    config.background = mode == "background"
    config.min_delay = config.max_delay = 0
    config.log_file = str(tmp_path / "runs.csv")
    answers = [response(config, "Student") for _ in range(config.count)]
    target = _save_plan(config, answers, tmp_path / "responses.json")
    session = Session(target, json.loads(target.read_text(encoding="utf-8")), digest(target))
    if mode == "resume":
        session.reconcile(1, True)
        config.count, answers = 1, answers[1:]
    messages = []
    choices = iter(["tabs", "open"] if mode == "background" else ["tabs" if mode == "tabs" else "wait"])
    ui = SimpleNamespace(
        _info=messages.append,
        _section=lambda text: None,
        _choice=lambda *args: args,
        _ask_select=lambda *args, **kwargs: next(choices),
    )
    runner = Runner(config, answers, ui, session)
    if fault == "capture":
        execute = driver.execute_script

        def broken_script(script, *args):
            if script == _COLLECT:
                raise OSError("PRIVATE")
            return execute(script, *args)

        monkeypatch.setattr(driver, "execute_script", broken_script)
    elif fault == "storage":
        monkeypatch.setattr(
            Journal, "record_followups", lambda *args: (_ for _ in ()).throw(OSError("PRIVATE"))
        )
    elif fault == "display":

        def broken_display(message):
            if message.startswith("Next steps"):
                raise OSError("Output closed")
            messages.append(message)

        ui._info = broken_display
    monkeypatch.setattr(inspector, "inspect_form", lambda *args: config)
    monkeypatch.setattr(execution, "config_from_schema", lambda value: value)
    monkeypatch.setattr(browser, "get_driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(driver, "quit", lambda: None)
    monkeypatch.setattr("gformbot.execution.sys.stdin", SimpleNamespace(isatty=lambda: True))
    real_confirmation = filler._positive_confirmation

    def manual_submit(d, *args):
        if not real_confirmation(d, *args):
            filler._button(d, "Submit").click()  # Simulate the human, on the local server only.
        return real_confirmation(d, *args)

    if mode in {"wait", "background", "halt"}:
        monkeypatch.setattr(filler, "_positive_confirmation", manual_submit)
    if mode == "tabs":
        ui._ask_text = lambda *args: filler._button(driver, "Submit").click()
    if mode == "halt":

        def stop(seconds):
            assert session.journal.states() == {1: "confirmed"}
            raise KeyboardInterrupt

        monkeypatch.setattr("gformbot.execution.time.sleep", stop)
    assert runner.run(submit=mode in {"auto", "resume", "batch"}, halt=mode == "halt", compact=True) is (
        mode != "fill"
    )
    records = session.journal.followups()
    if fault:
        assert len(accepted) == 1 and session.journal.states() == {1: "confirmed"}
        assert "PRIVATE" not in "\n".join(messages)
        if fault in {"capture", "storage"}:
            assert records == [] and any("could not" in m for m in messages)
        return
    assert {r["response"] for r in records} == ({2} if mode == "resume" else set(range(1, config.count + 1)))
    saved = json.dumps(records, ensure_ascii=False)
    assert "https://discord.gg/public" in saved and "usg=" not in saved
    assert f"{url.rsplit('/', 1)[0]}/students" in saved
    assert "/workers" not in saved and "Work directions" not in saved
    assert "Plain question instructions" in saved
    assert all(
        secret not in saved
        for secret in (
            "NEVER CAPTURE ANSWERS",
            "HIDDEN SECRET",
            "Account credentials",
            "Google footer",
            "PRIVATE",
            "edit2=",
        )
    )
    if mode == "fill":
        assert not accepted and not any(r["stage"] == "confirmation" for r in records)
    else:
        assert len(accepted) == config.count and all(
            state == "confirmed" for state in session.journal.states().values()
        )
        assert "https://chat.whatsapp.com/PublicInvite" in saved and "வணக்கம்" in saved
    output = "\n".join(messages)
    assert output.count("Next steps") == 1
    assert "Plain question instructions" not in output
    if mode == "batch":
        assert output.count("https://chat.whatsapp.com/PublicInvite") == 1


def test_collector_preserves_links_but_excludes_controls_and_nested_hidden_content(driver, html_page):
    html = """<div class="gubaDc"><p>First<br>Second <a href="/join">click here</a></p>
      <textarea>PRIVATE ANSWER</textarea><input value="PRIVATE INPUT">
      <span style="visibility:hidden">HIDDEN</span><span style="opacity:0">HIDDEN</span>
      <button>Submit</button></div>"""
    driver.get(html_page(html))
    notes = Recorder(Journal("batch"), "runs.csv", "Form")
    notes.observe(driver, 4, 1, "section")
    saved = json.dumps(notes.journal.followups())
    assert "click here" in saved and "First\\nSecond" in saved
    assert "PRIVATE" not in saved and "HIDDEN" not in saved and "Submit" not in saved
