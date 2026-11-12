import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gformbot import chatgpt_http


def event(value):
    return b"data: " + json.dumps(value).encode() + b"\n\n"


class Response:
    def __init__(self, *chunks, status=200, content_type="application/json"):
        self.chunks = iter(chunks)
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.closed = self.released = False

    def read1(self, _size):
        chunk = next(self.chunks, b"")
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class Pool:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cleared = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def clear(self):
        self.cleared = True


@pytest.fixture
def guest(monkeypatch):
    def create(*responses, **options):
        pool = Pool(responses)

        def pool_manager(**options):
            assert options["cert_reqs"] == "CERT_REQUIRED"
            pool.headers = options["headers"]
            return pool

        monkeypatch.setattr(chatgpt_http.urllib3, "PoolManager", pool_manager)
        return chatgpt_http.Client(**options), pool

    return create


def test_https_turn_sends_prompt_once_and_applies_assistant_stream_updates(guest):
    initial = {"author": {"role": "assistant"}, "content": {"parts": ["first draft"]}}
    replacement = {"p": "/message/content/parts/0", "o": "replace", "v": "Revised"}
    appended = {"p": "/message/content/parts/0", "o": "append", "v": " answer"}
    answer = Response(
        event({"v": {"message": initial}}),
        event({"o": "patch", "v": [replacement, appended]}),
        event({"v": " ✓"}) + b"data: [DONE]\n\n",
        content_type="text/event-stream; charset=utf-8",
    )
    requirements = Response(b'{"token":"setup-test"}')
    preparation = Response(b'{"conduit_token":"conduit-test"}')
    client, pool = guest(requirements, preparation, answer)
    messages = [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Task 🎯"}]

    assert client.ask(messages) == "Revised answer ✓"
    client.close()

    assert [url.rsplit("/", 1)[-1] for _, url, _ in pool.calls] == [
        "chat-requirements",
        "prepare",
        "conversation",
    ]
    for method, _, request in pool.calls:
        assert method == "POST"
        assert request["retries"] is request["redirect"] is False
    request = pool.calls[-1][2]
    body = json.loads(request["body"])
    assert body["history_and_training_disabled"] is True
    assert body["messages"][0]["content"]["parts"] == ["Rules\nTask 🎯"]
    assert request["headers"]["OpenAI-Sentinel-Chat-Requirements-Token"] == "setup-test"
    assert request["headers"]["x-conduit-token"] == "conduit-test"
    assert all(response.closed and response.released for response in (requirements, preparation, answer))
    assert pool.cleared


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_https_stops_at_complete_done_event_and_keeps_late_answer_text(guest, ending):
    initial = {
        "author": {"role": "assistant"},
        "content": {"parts": ["Initial [DONE] text"]},
        "status": "finished_successfully",
        "end_turn": True,
    }
    suffix = " ✓"
    late = b"data: " + json.dumps({"v": suffix}, ensure_ascii=False).encode() + ending * 2
    utf8 = late.index(suffix.encode()) + 2
    post_terminal = b": idle tail\n\n"
    answer = Response(
        event({"message": initial}),
        late[:utf8],
        late[utf8:] + b"data: [DO",
        b"NE]" + ending,
        ending,
        post_terminal,
        content_type="text/event-stream",
    )
    client, _ = guest(Response(b'{"token":"test"}'), Response(b'{"conduit_token":"test"}'), answer)

    assert client.ask([{"role": "user", "content": "Task"}]) == "Initial [DONE] text ✓"
    assert next(answer.chunks) == post_terminal
    assert answer.closed and answer.released


def test_finished_snapshot_does_not_hide_later_provider_limit(guest):
    message = {
        "author": {"role": "assistant"},
        "content": {"parts": ["Validated records"]},
        "status": "finished_successfully",
        "end_turn": True,
    }
    answer = Response(
        event({"message": message}),
        event({"error": {"status": 429, "code": "rate_limit_exceeded", "retry_after": 1}}),
        b"data: [DONE]\n\n",
        content_type="text/event-stream",
    )
    client, pool = guest(Response(b'{"token":"test"}'), Response(b'{"conduit_token":"test"}'), answer)

    with pytest.raises(chatgpt_http.ChatHTTPError) as exc:
        client.ask([{"role": "user", "content": "Task"}])

    assert (exc.value.status, exc.value.reason, exc.value.retry_after) == (429, "throttle", 1)
    assert exc.value.partial_content == "Validated records"
    assert len(pool.calls) == 3


@pytest.mark.parametrize(
    ("end", "message"),
    [(b"", "ended without a complete answer"), (TimeoutError(), "reached its time limit")],
)
def test_interrupted_generation_exposes_partial_output_without_replay(guest, end, message):
    answer = Response(event({"v": "complete record"}), end, content_type="text/event-stream")
    client, pool = guest(Response(b'{"token":"setup-test"}'), Response(b'{"conduit_token":"test"}'), answer)

    with pytest.raises(chatgpt_http.ChatHTTPError, match=message) as exc:
        client.ask([{"role": "user", "content": "Task"}])

    assert exc.value.partial_content == "complete record"
    assert exc.value.retryable
    assert len(pool.calls) == 3
    assert answer.closed and answer.released


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (Response(status=403), "denied setup"),
        (Response(status=429), "cause unclassified"),
        (Response(b'{"token":"test","proofofwork":{"required":true}}'), "requires guest verification"),
    ],
)
def test_blocked_setup_does_not_send_a_prompt(guest, response, message):
    client, pool = guest(response)

    with pytest.raises(chatgpt_http.ChatHTTPError, match=message) as exc:
        client.ask([{"role": "user", "content": "Task"}])

    assert not exc.value.retryable
    assert len(pool.calls) == 1
    assert "Task" not in pool.calls[0][2]["body"].decode()
    assert response.closed and response.released


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (Response(status=401), False),
        (Response(status=403), False),
        (Response(status=413), False),
        (Response(status=429), False),
        (Response(status=503), True),
        (Response(b"data: broken-json\n\n", content_type="text/event-stream"), False),
        (Response(event({"error": "rejected"}), content_type="text/event-stream"), False),
        (Response(b"data: [DONE]\n\n", content_type="text/event-stream"), False),
    ],
)
def test_generation_classifies_failures_without_retrying(guest, response, retryable):
    client, pool = guest(Response(b'{"token":"test"}'), Response(b'{"conduit_token":"test"}'), response)
    with pytest.raises(chatgpt_http.ChatHTTPError) as exc:
        client.ask([{"role": "user", "content": "Task"}])
    assert exc.value.retryable is retryable
    assert len(pool.calls) == 3
    assert response.closed and response.released


def test_network_setup_failure_is_retryable_but_not_retried_by_client(guest):
    client, pool = guest(OSError("connection dropped"))
    with pytest.raises(chatgpt_http.ChatHTTPError, match="connection failed during setup") as exc:
        client.ask([{"role": "user", "content": "Task"}])
    assert exc.value.retryable
    assert len(pool.calls) == 1


def test_cancel_stops_new_requests_without_clearing_shared_pool(guest):
    client, pool = guest()
    client.cancel()
    with pytest.raises(chatgpt_http.ChatHTTPError, match="cancelled") as exc:
        client.ask([{"role": "user", "content": "Task"}])
    assert not exc.value.retryable
    assert pool.calls == [] and not pool.cleared
    client.close()
    assert pool.cleared


def test_cancel_abandons_stream_and_preserves_partial_records(guest):
    response = Response(event({"v": "complete record"}), content_type="text/event-stream")
    client, pool = guest(Response(b'{"token":"test"}'), Response(b'{"conduit_token":"test"}'), response)
    read = response.read1

    def read_then_cancel(size):
        result = read(size)
        client.cancel()
        return result

    response.read1 = read_then_cancel
    with pytest.raises(chatgpt_http.ChatHTTPError, match="cancelled") as exc:
        client.ask([{"role": "user", "content": "Task"}])
    assert exc.value.partial_content == "complete record"
    assert not exc.value.retryable
    assert len(pool.calls) == 3 and not pool.cleared
    assert response.closed and response.released


def test_temporary_limit_waits_and_resumes_with_same_guest(guest):
    limited = Response(b'{"error":{"code":"slow_down"}}', status=429)
    limited.headers["Retry-After"] = "0.02"
    waits = []
    client, pool = guest(
        limited,
        Response(b'{"token":"test"}'),
        Response(b'{"conduit_token":"test"}'),
        Response(event({"v": "Recovered"}) + b"data: [DONE]\n\n", content_type="text/event-stream"),
        on_wait=waits.append,
    )
    start = time.monotonic()
    assert client.ask([{"role": "user", "content": "Task"}]) == "Recovered"
    assert time.monotonic() - start >= 0.02
    assert waits == [0.02]
    assert len({kw["headers"]["oai-device-id"] for _, _, kw in pool.calls}) == 1
    assert len(pool.calls) == 4


@pytest.mark.parametrize(
    ("payload", "retry_after", "reason"),
    [
        (b'{"error":{"type":"insufficient_quota","message":"do not echo"}}', "0", "quota"),
        (b'{"detail":"unexpected"}', None, "unknown"),
        (b'{"error":{"code":"slow_down"}}', "120", "throttle"),
    ],
)
def test_terminal_limit_preserves_diagnostics_and_does_not_retry(guest, payload, retry_after, reason):
    response = Response(payload, status=429)
    response.headers.update({"X-Request-ID": "req_fixture", "X-RateLimit-Reset-Requests": "2m"})
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    waits = []
    client, pool = guest(response, on_wait=waits.append)
    messages = [{"role": "user", "content": "Task"}]
    with pytest.raises(chatgpt_http.ChatHTTPError) as exc:
        client.ask(messages)
    error = exc.value
    assert (error.status, error.stage, error.reason) == (429, "setup", reason)
    assert error.request_id == "req_fixture"
    assert error.rate_reset == {"requests": "2m"}
    assert "do not echo" not in str(error)
    with pytest.raises(chatgpt_http.ChatHTTPError) as peer:
        client.ask(messages)
    assert peer.value is not error and peer.value.reason == reason
    assert len(pool.calls) == 1 and waits == []


def test_worker_in_an_active_turn_respects_peer_cooldown_before_next_post(guest):
    client, pool = guest(Response(b"{}"))
    deadline = time.monotonic() + 2
    client._ready(deadline, enter=True)
    error = chatgpt_http.ChatHTTPError("Slow down", status=429, reason="throttle")
    start = time.monotonic()
    assert client._defer(error, 0.03, deadline, can_retry=True)
    with ThreadPoolExecutor(1) as pool_executor:
        assert pool_executor.submit(client._post, "/next", {}, deadline, "preparation").result() == b"{}"
    client._leave()
    assert time.monotonic() - start >= 0.03
    assert len(pool.calls) == 1


def test_stream_access_error_is_not_a_throttle_even_with_retry_timing(guest):
    denied = Response(
        event({"error": {"status": 403, "code": "verification_required", "retry_after": 1}}),
        content_type="text/event-stream",
    )
    client, pool = guest(Response(b'{"token":"test"}'), Response(b'{"conduit_token":"test"}'), denied)
    with pytest.raises(chatgpt_http.ChatHTTPError, match="denied generation") as exc:
        client.ask([{"role": "user", "content": "Task"}])
    assert exc.value.status == 403 and exc.value.reason != "throttle"
    assert len(pool.calls) == 3
