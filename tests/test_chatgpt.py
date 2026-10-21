import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gformbot import chatgpt


def test_open_chat_is_visible_and_navigates_only_to_chatgpt(monkeypatch):
    events = []

    class Driver:
        def get(self, url):
            events.append(url)

        def quit(self):
            events.append("quit")

    def get_driver(browser, headless):
        events.append((browser, headless))
        return Driver()

    monkeypatch.setattr(chatgpt, "get_driver", get_driver)
    chatgpt.open_chat("chrome")
    assert events == [("chrome", False), "https://chatgpt.com/"]


def test_open_failure_closes_private_browser(monkeypatch):
    closed = []

    class Driver:
        def get(self, url):
            raise RuntimeError("Unavailable")

        def quit(self):
            closed.append(True)

    monkeypatch.setattr(chatgpt, "get_driver", lambda *args, **kwargs: Driver())
    with pytest.raises(RuntimeError):
        chatgpt.open_chat("chrome")
    assert closed == [True]


def test_cleanup_failure_does_not_mask_navigation_error(monkeypatch):
    class Driver:
        def get(self, url):
            raise RuntimeError("Navigation failed")

        def quit(self):
            raise chatgpt.WebDriverException("Session disappeared")

    monkeypatch.setattr(chatgpt, "get_driver", lambda *a, **kw: Driver())
    with pytest.raises(RuntimeError, match="Navigation failed"):
        chatgpt.open_chat("chrome")


def test_cancelled_navigation_closes_browser(monkeypatch):
    closed = []

    class Driver:
        def get(self, url):
            raise KeyboardInterrupt()

        def quit(self):
            closed.append(True)

    monkeypatch.setattr(chatgpt, "get_driver", lambda *a, **kw: Driver())
    with pytest.raises(KeyboardInterrupt):
        chatgpt.open_chat("chrome")
    assert closed == [True]


@pytest.mark.parametrize(
    "messages,timeout",
    [([], 1), ([{}], 1), ([{"content": 2}], 1), ([{"content": "text"}], 0), ([{"content": "text"}], True)],
)
def test_invalid_request_fails_before_touching_browser(messages, timeout):
    assert pytest.raises(chatgpt.ChatGPTError, chatgpt.ask, None, messages, timeout=timeout)


def test_editor_lookup_survives_replaced_control():
    class Element:
        def __init__(self, stale):
            self.stale = stale

        def is_displayed(self):
            if self.stale:
                raise chatgpt.StaleElementReferenceException()
            return True

        def is_enabled(self):
            return True

    visible = Element(False)

    class Driver:
        def find_elements(self, *args):
            return [Element(True), visible]

    assert chatgpt._editor(Driver()) is visible


@pytest.mark.browser
@pytest.mark.skipif(
    os.environ.get("GFORMBOT_BROWSER_TESTS") != "1",
    reason="Set GFORMBOT_BROWSER_TESTS=1 for local Chrome checks.",
)
@pytest.mark.parametrize("editor", ["textarea", "contenteditable"])
def test_website_prompt_streaming_and_second_reply(editor):
    control = (
        '<textarea placeholder="Ask ChatGPT"></textarea>'
        if editor == "textarea"
        else '<div contenteditable="true" id="prompt-textarea"></div>'
    )
    markup = """<!doctype html><html><body>
        <h1>Local chat fixture</h1><ol><li data-message-role="assistant"><div data-assistant-markdown>OLD RESPONSE</div></li></ol>
        CONTROL <button aria-label="Send message" disabled>Send</button><script>
        const editor = document.querySelector('textarea,[contenteditable]');
        const send = document.querySelector('button');
        window.prompts = [];
        editor.addEventListener('input', () => send.disabled = false);
        send.onclick = () => {
            window.prompts.push(editor.tagName === 'TEXTAREA' ? editor.value : editor.innerText);
            send.disabled = true;
            const message = document.createElement('li');
            message.setAttribute('data-message-role', 'assistant');
            message.setAttribute('data-message-streaming', '');
            message.innerHTML = '<div data-assistant-markdown><pre><code>{"partial":true}</code></pre></div>';
            document.querySelector('ol').appendChild(message);
            setTimeout(() => {
                message.querySelector('code').textContent = JSON.stringify({responses:[{email:'test@example.com',date:'2026-10-12 14:30'}]});
                message.removeAttribute('data-message-streaming');
                send.disabled = false;
            }, 250);
        };</script></body></html>""".replace("CONTROL", control)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(markup.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from gformbot.browser import get_driver

    driver = None
    try:
        driver = get_driver("chrome", headless=True)
        driver.get(f"http://127.0.0.1:{server.server_port}/")
        messages = [{"content": "Generate JSON only.\nPreserve exact answers."}, {"content": '{"count":1}'}]
        for _ in range(2):
            result = json.loads(chatgpt.ask(driver, messages, {}, 5))
            assert result == {"responses": [{"email": "test@example.com", "date": "2026-10-12 14:30"}]}
        assert (
            driver.execute_script("return window.prompts") == ["\n".join(m["content"] for m in messages)] * 2
        )
    finally:
        if driver:
            driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
