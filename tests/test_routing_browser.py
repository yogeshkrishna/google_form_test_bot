import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from test_routing import branch_items, schema

from gformbot.config import QuestionConfig, RunConfig, config_from_schema
from gformbot.filler import SubmissionUncertainError, execute_response
from gformbot.generator import validate_answer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable local Chrome fixtures."
    ),
]


@pytest.fixture(scope="module")
def route_browser():
    from gformbot.browser import get_driver

    driver = get_driver("chrome")
    yield driver
    driver.quit()


@pytest.fixture
def branching_server():
    content = (Path(__file__).parent / "fixtures" / "branching_form.html").read_bytes()
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/formResponse":
                accepted.append(json.loads(parse_qs(parsed.query)["answers"][0]))
                result = b'<html><body><div class="vHW8K">Recorded</div></body></html>'
            else:
                result = content
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/viewform", accepted
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def configuration(url, mode=""):
    items = branch_items()
    items[0][3] = 2 if mode == "radio" else 3
    if mode == "empty":
        items.pop(2)
    config = config_from_schema(schema(items))
    config.url = url + ("?mode=" + mode if mode else "")
    return config


def response(config, category):
    supplied = {"1": category, "2": "Math", "3": "Teacher", "4": "Hi"}
    return validate_answer(config, {q.index: supplied[q.entry_id] for q in config.questions})


@pytest.mark.parametrize(
    "category,mode,expected",
    [
        ("Student", "", {"entry.1": "Student", "entry.2": "Math", "entry.4": "Hi"}),
        ("Worker", "", {"entry.1": "Worker", "entry.3": "Teacher", "entry.4": "Hi"}),
        ("Skip", "", {"entry.1": "Skip"}),
        ("Student", "empty", {"entry.1": "Student", "entry.4": "Hi"}),
        ("Student", "radio", {"entry.1": "Student", "entry.2": "Math", "entry.4": "Hi"}),
        ("Skip", "radio", {"entry.1": "Skip"}),
    ],
)
def test_browser_submits_only_the_selected_route(route_browser, branching_server, category, mode, expected):
    url, accepted = branching_server
    config = configuration(url, mode)
    intents = []
    result = execute_response(
        route_browser,
        config,
        response(config, category),
        submit=True,
        before_submit=lambda: intents.append(1),
    )
    assert result["status"] == "confirmed"
    assert intents == [1] and accepted == [expected]
    assert len(result["filled_questions"]) == len(expected)


@pytest.mark.parametrize("mode,category", [("wrong", "Student"), ("early", "Student"), ("missing", "Skip")])
def test_unexpected_navigation_stops_before_submit(route_browser, branching_server, mode, category):
    url, accepted = branching_server
    config = configuration(url, mode)
    intents = []
    with pytest.raises(RuntimeError, match="planned|route|Submit"):
        execute_response(
            route_browser,
            config,
            response(config, category),
            submit=True,
            timeout=1,
            before_submit=lambda: intents.append(1),
        )
    assert intents == [] and accepted == []


def test_branching_review_fills_without_submitting(route_browser, branching_server):
    url, accepted = branching_server
    config = configuration(url)
    result = execute_response(route_browser, config, response(config, "Worker"), submit=False)
    assert result["status"] == "filled" and result["filled_questions"] == [1, 3, 4]
    assert accepted == [] and route_browser.find_element("id", "submit").is_displayed()


@pytest.mark.parametrize("submit", [False, True])
@pytest.mark.parametrize("google_identity", [False, True])
@pytest.mark.parametrize("outcome", ["confirmation", "another_page"])
def test_early_terminal_google_next_has_a_submission_boundary(
    route_browser, html_page, submit, google_identity, outcome
):
    identity = 'jsname="OCpkoe"' if google_identity else ""
    url = html_page(
        '<div role="listitem"><h2 role="heading">Exit</h2>'
        '<input type="radio" name="entry.1" value="Finish" aria-label="Finish"></div>'
        f'<button type="button" {identity} onclick="finish()">Next</button>'
        "<script>window.finalClicks=0;function finish(){window.finalClicks++;"
        "history.pushState({},'', 'formResponse');"
        "document.body.innerHTML=NEXT_DOCUMENT;}</script>".replace(
            "NEXT_DOCUMENT",
            json.dumps(
                "<div data-gformbot-confirmation>Recorded</div>"
                if outcome == "confirmation"
                else '<div role="listitem"><h2 role="heading">Unexpected page</h2><input></div>'
            ),
        )
    )
    config = RunConfig(
        url,
        sections=["Exit", "Unvisited"],
        section_next=[2, 0],
        questions=[
            QuestionConfig(1, "Exit", "radio", True, ["Finish"], entry_id="1", branches={"Finish": 0})
        ],
    )
    intents = []

    def before_submit():
        assert route_browser.execute_script("return window.finalClicks") == 0
        intents.append("intent")

    if not google_identity:
        with pytest.raises(RuntimeError, match="Submit"):
            execute_response(
                route_browser, config, {1: "Finish"}, submit=submit, timeout=0.5, before_submit=before_submit
            )
        assert intents == [] and route_browser.execute_script("return window.finalClicks") == 0
    elif submit and outcome == "another_page":
        with pytest.raises(SubmissionUncertainError, match="acceptance"):
            execute_response(
                route_browser, config, {1: "Finish"}, submit=True, timeout=0.5, before_submit=before_submit
            )
        assert intents == ["intent"] and route_browser.execute_script("return window.finalClicks") == 1
    else:
        result = execute_response(
            route_browser, config, {1: "Finish"}, submit=submit, timeout=2, before_submit=before_submit
        )
        assert result["status"] == ("confirmed" if submit else "filled")
        assert intents == (["intent"] if submit else [])
        assert route_browser.execute_script("return window.finalClicks") == int(submit)
