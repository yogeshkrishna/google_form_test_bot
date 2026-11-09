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

    def get_driver(browser):
        events.append(browser)
        return Driver()

    monkeypatch.setattr(chatgpt, "get_driver", get_driver)
    chatgpt.open_chat("chrome")
    assert events == ["chrome", "https://chatgpt.com/"]


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


def test_new_conversation_reuses_browser_and_requires_old_messages_to_disappear(monkeypatch):
    events = []

    class Driver:
        def get(self, url):
            events.append(url)

    driver = Driver()

    class Wait:
        def __init__(self, actual, *args, **kwargs):
            assert actual is driver

        def until(self, ready):
            assert ready(driver) is False
            assert ready(driver) is True

    messages = iter([[object()], []])
    monkeypatch.setattr(chatgpt, "_messages", lambda d: next(messages))
    monkeypatch.setattr(chatgpt, "WebDriverWait", Wait)
    chatgpt.new_chat(driver)
    assert events == ["https://chatgpt.com/"]


def test_new_conversation_failure_stops_without_sending(monkeypatch):
    class Driver:
        def get(self, url):
            raise chatgpt.WebDriverException("Offline")

    with pytest.raises(chatgpt.ChatGPTError, match="fresh ChatGPT conversation"):
        chatgpt.new_chat(Driver())


@pytest.fixture
def website(monkeypatch):
    """A deterministic message stream without a browser or real waiting."""
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0)
    state = SimpleNamespace(prompt="", sends=0, streaming=False, content="", old=False, alert="")

    class Element:
        tag_name = "textarea"

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            pass

        def get_attribute(self, name):
            return state.prompt

    class Send(Element):
        def click(self):
            state.sends += 1

    class Message:
        text = "old response"

        def __init__(self, old=False):
            self.old = old

        def get_dom_attribute(self, name):
            return "" if not self.old and state.streaming else None

        def find_elements(self, by, selector):
            text = self.text if self.old else state.content
            return [SimpleNamespace(get_attribute=lambda name: text)] if selector == "pre code" else []

    class Driver:
        def execute_script(self, code, editor, prompt):
            state.prompt = prompt

        def find_elements(self, by, selector):
            if selector.startswith("#prompt"):
                return [Element()]
            if "data-message-author-role" in selector:
                old = [Message(old=True)] if state.old else []
                return old + ([Message()] if state.sends and state.content else [])
            if "send-button" in selector:
                return [Send()]
            if "stop-button" in selector:
                return [Element()] if state.streaming else []
            if selector == "[role='alert']" and state.alert:
                return [SimpleNamespace(text=state.alert, is_displayed=lambda: True)]
            return []

    class Wait:
        def __init__(self, driver, timeout, **kwargs):
            self.driver, self.timeout = driver, timeout

        def until(self, ready):
            deadline = clock.now + self.timeout
            while clock.now < deadline:
                result = ready(self.driver)
                if result:
                    return result
                clock.now += 0.2
            raise chatgpt.TimeoutException()

    monkeypatch.setattr(chatgpt.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(chatgpt, "WebDriverWait", Wait)
    return Driver(), state, clock


def test_ask_waits_for_stable_reply_and_sends_only_current_prompt(website):
    driver, state, clock = website
    state.content = '{"profile_id":7,"answers":{"1":"A"}}'
    result = chatgpt.ask(driver, [{"content": "Create profile 7 using the previous form."}], timeout=2)
    assert result == state.content
    assert state.prompt == "Create profile 7 using the previous form."
    assert state.sends == 1
    assert clock.now >= 0.6


def test_timeout_exposes_only_new_partial_content_without_resending(website):
    driver, state, _ = website
    state.old = True
    state.streaming = True
    state.content = '{"profile_id":1,"answers":{"1":"A"}}\n{"profile_id":2'
    with pytest.raises(chatgpt.ChatGPTError) as failure:
        chatgpt.ask(driver, [{"content": "Create profiles 1 and 2."}], timeout=1)
    assert failure.value.partial_content == state.content
    assert state.sends == 1


def test_missing_reply_never_salvages_previous_conversation_message(website):
    driver, state, _ = website
    state.old = True
    with pytest.raises(chatgpt.ChatGPTError) as failure:
        chatgpt.ask(driver, [{"content": "Next batch."}], timeout=1)
    assert failure.value.partial_content == ""
    assert state.sends == 1


def test_site_limit_alert_stops_without_resending(website):
    driver, state, _ = website
    state.alert = "Message limit reached"
    with pytest.raises(chatgpt.ChatGPTError, match="Message limit reached"):
        chatgpt.ask(driver, [{"content": "Next batch."}], timeout=1)
    assert state.sends == 1


def test_site_alert_after_partial_reply_is_fatal_even_without_streaming_controls(website):
    driver, state, _ = website
    state.old = True
    state.content = '{"profile_id":1,"answers":{"1":"A"}}\n{"profile_id":2'
    state.alert = "Connection lost"
    with pytest.raises(chatgpt.ChatGPTError, match="Connection lost") as failure:
        chatgpt.ask(driver, [{"content": "Create profiles 1 and 2."}], timeout=2)
    assert failure.value.partial_content == state.content
    assert state.sends == 1


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
        driver = get_driver("chrome")
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
