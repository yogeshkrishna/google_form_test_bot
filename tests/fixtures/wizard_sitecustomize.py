"""Provide offline transport boundaries for the installed wizard subprocess."""

import json
import os
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import parse_qs


def _item(key, title, kind, choices=()):
    options = [[value, None, None, None, 0] for value in choices]
    return [100 + key, title, None, kind, [[key, options or None, 1]]]


def _block_network(event, arguments):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        address = arguments[1] if event == "socket.connect" else arguments[0]
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("localhost", "127.0.0.1", "::1", None):
            raise AssertionError(f"Unexpected network access in installed-wizard test: {event} {host}")


def _wait_for_terminal_input():
    """Wait for the test to queue a key without consuming it."""
    if os.name == "nt":
        from msvcrt import kbhit

        deadline = monotonic() + 10
        while monotonic() < deadline:
            if kbhit():
                return
            sleep(0.01)
    else:
        from select import select

        if select([sys.stdin], [], [], 10)[0]:
            return
    raise AssertionError("No terminal input was queued during the fixture pause.")


def _install():
    sys.addaudithook(_block_network)
    import gformbot
    from gformbot import chatgpt_http, execution, inspector, transport

    if not Path(gformbot.__file__).is_relative_to(Path(sys.prefix)):
        raise AssertionError(f"Expected the installed wheel, loaded {gformbot.__file__}")
    fixture_url = os.environ["GFORMBOT_WIZARD_FIXTURE_URL"]
    trace = Path("calls.jsonl")
    lock = Lock()
    fields = [_item(1, "Choice", 2, ("A", "B")), _item(2, "Describe the handoff", 1)]
    metadata = ["Offline smoke test", fields, ["Recorded by the fixture"]] + [None] * 5 + ["Wizard fixture"]
    document = (
        f"<script>var FB_PUBLIC_LOAD_DATA_ = {json.dumps([None, metadata])};</script>"
        '<input type="hidden" name="fbzx" value="fixture-token">'
        '<input type="hidden" name="pageHistory" value="0">'
    )

    def record(event, **data):
        with lock, trace.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": event, **data}) + "\n")

    def inspect(request, **_):
        assert request.full_url == fixture_url
        record("inspect")
        response = BytesIO(document.encode())
        response.url, response.headers = fixture_url, Message()
        return response

    def forms_request(_client, method, url, body=None):
        if method == "GET" and url == fixture_url:
            return 200, {}, document
        assert method == "POST" and url == fixture_url.replace("/viewform", "/formResponse")
        fields = parse_qs(body.decode(), keep_blank_values=True)
        record("submit", choice=fields["entry.1"][0], answer=fields["entry.2"][0])
        return 200, {}, '<div class="vHW8K">Recorded by the fixture</div>'

    def ask(_client, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        if "profiles" in task:
            record("review", ids=list(task["profiles"]))
            return "\n".join(json.dumps({"response": int(key), "issues": []}) for key in task["profiles"])
        ids = task["profile_numbers"]
        record("generate", ids=ids, guide=task.get("reference"), instructions=task.get("instructions"))
        answers = task["answer_schema"]["properties"]
        rejected = os.environ.get("GFORMBOT_WIZARD_FIXTURE_INCOMPLETE") == "1"
        lines = []
        for key in ids:
            choice = 99 if rejected and key == 2 else key % 2 + 1
            values = {"1": choice, "2": f"Fixture answer {key} records a distinct handoff."}
            lines.append(json.dumps({"profile_id": key, "answers": {k: values[k] for k in answers}}))
        return "\n".join(lines)

    def wait(_delay):
        if os.environ.get("GFORMBOT_WIZARD_FIXTURE_INTERRUPT") == "1":
            record("pause")
            sleep(60)
        if os.environ.get("GFORMBOT_WIZARD_FIXTURE_QUEUED_ENTER") == "1":
            print("Fixture waiting for a queued key.", flush=True)
            _wait_for_terminal_input()

    inspector.urlopen = inspect
    transport.Client._request = forms_request
    chatgpt_http.Client.ask = ask
    execution.time = SimpleNamespace(sleep=wait)


if __name__ == "sitecustomize":
    try:
        _install()
    except Exception as error:
        # Normal sitecustomize exceptions are logged and ignored by Python.
        # Abort instead so this fixture can never fall through to live services.
        raise SystemExit("Could not install wizard transport fixtures") from error
