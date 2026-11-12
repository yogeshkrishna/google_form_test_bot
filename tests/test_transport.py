"""HTTP protocol and acceptance boundaries, using only in-memory responses."""

import json
import subprocess
import sys
import textwrap
from urllib.parse import parse_qs

import pytest
from test_inspector import URL, document, item
from test_routing import branch_items

from gformbot import transport
from gformbot.access import FormAccessError, SubmissionRejectedError, SubmissionUncertainError
from gformbot.config import QuestionConfig, config_from_schema
from gformbot.inspector import parse_form_html

FIELDS = '<input type="hidden" name="fbzx" value="fixture-token"><input type="hidden" name="pageHistory" value="0">'
CONFIRMED = '<div class="vHW8K">Thanks for testing!</div>'


class Response:
    def __init__(self, status, document, headers=None):
        self.status, self.headers = status, headers or {}
        self.data = document.encode()
        self.released = self.closed = False

    def read(self, limit):
        return self.data[:limit]

    def release_conn(self):
        self.released = True

    def close(self):
        self.closed = True


class Pool:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cleared = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def clear(self):
        self.cleared = True


def fixture_client(monkeypatch, *responses, items=None, markup=None):
    markup = (
        document(items if items is not None else [item(1, "Answer", 0)]) + FIELDS
        if markup is None
        else markup
    )
    config = config_from_schema(parse_form_html(markup, URL))
    pool = Pool([Response(200, markup), *responses])
    options = []
    monkeypatch.setattr(transport.urllib3, "PoolManager", lambda **kwargs: options.append(kwargs) or pool)
    return config, pool, options


def test_one_pool_submits_exact_profiles_without_retrying_or_following_post_redirects(monkeypatch):
    config, pool, options = fixture_client(monkeypatch, Response(200, CONFIRMED), Response(200, CONFIRMED))
    intents = []
    with transport.Client(config) as client:
        for answer in ("Roads, footpaths | 😀", "[literal] =SUM(1,2)"):
            assert client.submit({1: answer}, before_submit=lambda: intents.append(len(pool.calls)))[0]
    assert intents == [1, 2]
    assert [call[0] for call in pool.calls] == ["GET", "POST", "POST"]
    assert options[0]["retries"] is False
    assert options[0]["cert_reqs"] == "CERT_REQUIRED"
    for _, _, kwargs in pool.calls:
        assert kwargs["retries"] is False and kwargs["redirect"] is False
    assert parse_qs(pool.calls[1][2]["body"].decode())["entry.1"] == ["Roads, footpaths | 😀"]
    assert parse_qs(pool.calls[2][2]["body"].decode())["entry.1"] == ["[literal] =SUM(1,2)"]
    assert pool.cleared


@pytest.mark.parametrize(
    "answer,history,present,absent",
    [
        ({1: "Student", 2: "Math", 4: "Hello"}, "0,1,3", {"entry.2", "entry.4"}, {"entry.3"}),
        ({1: "Worker", 3: "Teacher", 4: "Hello"}, "0,2,3", {"entry.3", "entry.4"}, {"entry.2"}),
        ({1: "Skip"}, "0", {"entry.1"}, {"entry.2", "entry.3", "entry.4"}),
    ],
)
def test_only_the_selected_route_is_encoded(monkeypatch, answer, history, present, absent):
    config, pool, _ = fixture_client(monkeypatch, Response(200, CONFIRMED), items=branch_items())
    with transport.Client(config) as client:
        client.submit(answer)
    fields = parse_qs(pool.calls[1][2]["body"].decode(), keep_blank_values=True)
    assert fields["pageHistory"] == [history]
    assert present <= fields.keys() and not absent & fields.keys()
    assert json.loads(fields["partialResponse"][0]) == [None, None, "fixture-token"]


@pytest.mark.parametrize(
    "question,value,expected",
    [
        (
            QuestionConfig(1, "Choices", "checkbox", entry_id="1"),
            ["Roads, paths", "Trees"],
            {"entry.1": ["Roads, paths", "Trees"]},
        ),
        (
            QuestionConfig(1, "Choice", "radio", entry_id="1", allow_other=True),
            {"choices": [], "other": "Rust, C++"},
            {"entry.1": ["__other_option__"], "entry.1.other_option_response": ["Rust, C++"]},
        ),
        (
            QuestionConfig(1, "Date", "date", entry_id="1", date_include_time=True),
            "2028-02-29 00:05",
            {
                "entry.1_year": ["2028"],
                "entry.1_month": ["2"],
                "entry.1_day": ["29"],
                "entry.1_hour": ["00"],
                "entry.1_minute": ["05"],
            },
        ),
        (
            QuestionConfig(1, "Date", "date", entry_id="1", date_include_year=False),
            "02-29",
            {"entry.1_month": ["2"], "entry.1_day": ["29"]},
        ),
        (
            QuestionConfig(1, "Clock", "time", entry_id="1"),
            "00:05",
            {"entry.1_hour": ["00"], "entry.1_minute": ["05"]},
        ),
        (
            QuestionConfig(1, "Elapsed", "duration", entry_id="1"),
            "27:15:03",
            {"entry.1_hour": ["27"], "entry.1_minute": ["15"], "entry.1_second": ["03"]},
        ),
        (
            QuestionConfig(1, "Email", "text", entry_id="emailAddress"),
            "fictional@example.test",
            {"emailAddress": ["fictional@example.test"]},
        ),
    ],
)
def test_google_field_encoding_preserves_complete_values(question, value, expected):
    from urllib.parse import urlencode

    fields = parse_qs(urlencode(transport._values(question, value)), keep_blank_values=True)
    for key, values in expected.items():
        assert fields[key] == values
    if question.qtype in {"checkbox", "radio"}:
        assert fields["entry.1_sentinel"] == [""]


def test_local_http_validation_sends_no_post_and_has_no_confirmation(monkeypatch):
    config, pool, _ = fixture_client(monkeypatch)
    observed = []
    with transport.Client(config) as client:
        assert client.validate({1: "Exact answer"}, observe=lambda *args: observed.append(args))[0]
    assert [call[0] for call in pool.calls] == ["GET"]
    assert all(stage != "confirmation" for _, stage, _ in observed)


def test_journal_intent_can_abort_before_any_post(monkeypatch):
    config, pool, _ = fixture_client(monkeypatch)

    def unavailable():
        raise OSError("journal unavailable")

    with transport.Client(config) as client, pytest.raises(OSError, match="journal unavailable"):
        client.submit({1: "Answer"}, before_submit=unavailable)
    assert [call[0] for call in pool.calls] == ["GET"]


@pytest.mark.parametrize(
    "response",
    [
        Response(200, "<h1>An unfamiliar result</h1>"),
        Response(200, '<script>var text="Thanks for testing!"</script>'),
        Response(200, '<div hidden class="vHW8K">Thanks for testing!</div>'),
        Response(200, CONFIRMED + '<input name="entry.1">'),
        Response(200, CONFIRMED + '<div class="g-recaptcha"></div>'),
        Response(200, '<div class="vHW8K">Different confirmation</div>'),
        Response(200, "<h1>You have already responded</h1>"),
        Response(403, "<h1>You need permission</h1>"),
        Response(503, ""),
        OSError("private transport details"),
    ],
)
def test_unproven_acceptance_is_uncertain_and_never_retried(monkeypatch, response):
    config, pool, _ = fixture_client(monkeypatch, response)
    observed, intents = [], []
    with transport.Client(config) as client, pytest.raises(SubmissionUncertainError):
        client.submit(
            {1: "Answer"},
            before_submit=lambda: intents.append(True),
            observe=lambda *args: observed.append(args),
        )
    assert intents == [True]
    assert [call[0] for call in pool.calls] == ["GET", "POST"]
    assert all(stage != "confirmation" for _, stage, _ in observed)
    assert pool.cleared


@pytest.mark.parametrize(
    "document,status", [(FIELDS, 200), ('<div role="alert">Invalid answer</div>', 200), ("", 400)]
)
def test_definitive_rejection_remains_distinct_from_uncertainty(monkeypatch, document, status):
    config, pool, _ = fixture_client(monkeypatch, Response(status, document))
    with transport.Client(config) as client, pytest.raises(SubmissionRejectedError):
        client.submit({1: "Answer"})
    assert [call[0] for call in pool.calls] == ["GET", "POST"]


def test_accepted_followups_are_observed_only_after_positive_confirmation(monkeypatch):
    response = CONFIRMED.replace(
        "Thanks for testing!", 'Thanks for testing!<a href="https://discord.gg/public"></a>'
    )
    config, pool, _ = fixture_client(monkeypatch, Response(200, response))
    observed = []
    with transport.Client(config) as client:
        assert client.submit({1: "Answer"}, observe=lambda *args: observed.append(args))[0]
    assert observed[-1] == (
        0,
        "confirmation",
        [{"text": "Thanks for testing!", "links": ["https://discord.gg/public"]}],
    )
    assert len(pool.calls) == 2


def test_followup_storage_failure_cannot_undo_acceptance(monkeypatch):
    config, pool, _ = fixture_client(monkeypatch, Response(200, CONFIRMED))

    def observe(section, stage, blocks):
        if stage == "confirmation":
            raise OSError("closed storage")

    with transport.Client(config) as client:
        assert client.submit({1: "Answer"}, observe=observe)[0]
    assert [call[0] for call in pool.calls] == ["GET", "POST"]


@pytest.mark.parametrize(
    "destination", [URL, "https://accounts.google.com/ServiceLogin", "https://other.test/formResponse"]
)
def test_submission_redirect_outside_confirmation_is_uncertain_without_following(monkeypatch, destination):
    config, pool, _ = fixture_client(monkeypatch, Response(302, "", {"Location": destination}))
    with transport.Client(config) as client, pytest.raises(SubmissionUncertainError):
        client.submit({1: "Answer"})
    assert [call[0] for call in pool.calls] == ["GET", "POST"]


def test_same_form_confirmation_redirect_uses_one_get_and_never_repeats_post(monkeypatch):
    config, pool, _ = fixture_client(
        monkeypatch, Response(303, "", {"Location": "./formResponse?done=1"}), Response(200, CONFIRMED)
    )
    with transport.Client(config) as client:
        assert client.submit({1: "Answer"})[0]
    assert [call[0] for call in pool.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("restriction", ["requires_signin", "one_response", "email_collection"])
def test_account_restrictions_fail_before_constructing_a_connection_pool(monkeypatch, restriction):
    config = config_from_schema(parse_form_html(document([item(1, "Answer", 0)]), URL))
    setattr(config, restriction, "verified" if restriction == "email_collection" else True)
    monkeypatch.setattr(
        transport.urllib3, "PoolManager", lambda **kw: pytest.fail("Restricted client created a pool")
    )
    with pytest.raises(FormAccessError):
        transport.Client(config)


@pytest.mark.parametrize("mutation", ["schema", "instructions", "missing_fields", "challenge"])
def test_changed_or_unrecognized_snapshot_stops_before_post_and_closes_pool(monkeypatch, mutation):
    initial = document([item(1, "Answer", 0)]) + FIELDS
    config = config_from_schema(parse_form_html(initial, URL))
    current = initial
    if mutation == "schema":
        current = document([item(1, "Changed question", 0)]) + FIELDS
    elif mutation == "instructions":
        config.instructions = [{"page": 1, "title": "Old directions"}]
    elif mutation == "missing_fields":
        current = document([item(1, "Answer", 0)])
    else:
        current += '<div class="g-recaptcha"></div>'
    pool = Pool([Response(200, current)])
    monkeypatch.setattr(transport.urllib3, "PoolManager", lambda **kw: pool)
    with pytest.raises(ValueError):
        transport.Client(config)
    assert [call[0] for call in pool.calls] == ["GET"]
    assert pool.cleared


def test_supplied_csv_full_cli_runs_without_ai_or_browser_automation(tmp_path):
    script = textwrap.dedent("""
        import importlib.abc
        import json
        import sys
        from email.message import Message
        from io import BytesIO
        from pathlib import Path

        forbidden = ("selenium", "gformbot.browser", "gformbot.selenium_runtime", "gformbot._vendor.selenium", "gformbot.chatgpt", "gformbot.chatgpt_http")
        class Guard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                    raise AssertionError("Supplied data imported " + fullname)
        sys.meta_path.insert(0, Guard())
        from click.testing import CliRunner
        from gformbot import cli, inspector, transport

        url = "https://docs.google.com/forms/d/e/offline_fixture/viewform"
        metadata = ["Description", [[101, "Answer", None, 0, [[1, None, 1]]]], ["Recorded"]] + [None] * 5 + ["Form"]
        document = "<script>FB_PUBLIC_LOAD_DATA_=" + json.dumps([None, metadata]) + ";</script>"
        document += '<input type="hidden" name="fbzx" value="fixture"><input type="hidden" name="pageHistory" value="0">'
        class ReadResponse(BytesIO):
            def __init__(self):
                super().__init__(document.encode())
                self.url, self.headers = url, Message()
        inspector.urlopen = lambda *a, **kw: ReadResponse()
        calls = []
        def request(self, method, target, body=None):
            calls.append((method, body))
            return 200, {}, document if method == "GET" else '<div class="vHW8K">Recorded</div>'
        transport.Client._request = request
        cli._ask_confirm = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Unexpected Yes prompt"))
        cli._ask_select = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Unexpected review menu"))
        data = Path("supplied.csv")
        data.write_text("Q1\\nExact imported profile\\n", encoding="utf-8")
        before = data.read_bytes()
        result = CliRunner().invoke(cli.cli, ["generate", url, "--data", str(data)])
        assert result.exit_code == 0, repr(result.exception) + result.output
        assert [method for method, _ in calls] == ["GET", "POST"]
        assert b"Exact+imported+profile" in calls[-1][1]
        assert data.read_bytes() == before
        assert not any(name in sys.modules for name in forbidden)
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
