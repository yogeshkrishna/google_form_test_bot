"""Exercise the loopback editor and durable edits without opening an external site."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from gformbot import cli
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig
from gformbot.editor import EditorServer
from gformbot.state import Journal, Session, atomic_json, batch_id


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=3,
        form_title='<script>alert("not executed")</script>',
        questions=[
            QuestionConfig(1, "Choice", "radio", True, options=["A", "B"]),
            QuestionConfig(2, "Reason", "text", True),
        ],
    )
    answers = [{1: "A", 2: f"Original {i}"} for i in range(3)]
    path = cli._save_plan(c, answers)
    return c, answers, path


@contextmanager
def running(c, answers, save=None, statuses=None):
    with EditorServer(c, answers, save or (lambda i, row: row), statuses) as editor:
        thread = threading.Thread(target=editor.run)
        thread.start()
        try:
            yield editor
        finally:
            editor.closed = True
            thread.join(6)
            assert not thread.is_alive()


def request(editor, action="state", body=None, **headers):
    values = {"Origin": editor.origin, "Content-Type": "application/json", **headers}
    req = Request(
        editor.url + action, data=None if body is None else json.dumps(body).encode(), headers=values
    )
    try:
        with urlopen(req, timeout=5) as response:
            data = response.read()
            return response.status, json.loads(data) if action else data, response.headers
    except HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def test_local_editor_escapes_data_and_limits_access(batch):
    c, answers, _ = batch
    with running(c, answers) as editor:
        code, html, headers = request(editor, "")
        assert code == 200 and c.form_title.encode() not in html
        assert headers["Cache-Control"] == "no-store" and "sha256-" in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "no-referrer"
        assert request(editor)[1]["title"] == c.form_title
        assert request(editor, body={}, Origin="https://unrelated.invalid")[0] == 403
        assert request(editor, Host="unrelated.invalid")[0] == 404
        assert request(editor, "../state")[0] == 404
        assert (
            request(editor, "save", {"index": 1, "answers": answers[0]}, **{"Content-Type": "text/plain"})[0]
            == 403
        )


def test_editor_validates_each_response_and_close_does_not_save(batch):
    c, answers, _ = batch
    writes = []
    with running(c, answers, lambda i, row: writes.append((i, row)) or row) as editor:
        invalid = request(editor, "save", {"index": 1, "answers": {1: "B", 2: ""}})
        assert invalid[0] == 400 and invalid[1]["question"] == 2 and writes == []
        code, response, _ = request(editor, "save", {"index": 2, "answers": {1: "B", 2: "Updated"}})
        assert code == 200 and response["answers"] == {"1": "B", "2": "Updated"}
        assert writes == [(2, {1: "B", 2: "Updated"})]
        assert answers[1][2] == "Original 1"
        assert request(editor, "close", {})[0] == 200
        assert len(writes) == 1


def test_journal_failure_is_reported_without_losing_editor_answers(batch):
    c, answers, _ = batch

    def fail(*args):
        raise sqlite3.OperationalError("database is locked")

    with running(c, answers, fail) as editor:
        status, data, _ = request(editor, "save", {"index": 1, "answers": {1: "B", 2: "Updated"}})
        assert status == 409 and "reopen the batch" in data["error"]
        assert request(editor, "response?index=1")[1]["answers"]["2"] == answers[0][2]


@pytest.mark.parametrize("state", ["confirmed", "uncertain"])
def test_protected_responses_are_read_only_including_status_changes(batch, state):
    c, answers, _ = batch
    statuses = ["ready"] * 3
    with running(
        c, answers, lambda *a: pytest.fail("Protected response was saved"), lambda: statuses
    ) as editor:
        assert request(editor, "response?index=1")[1]["status"] == "ready"
        statuses[0] = state
        assert request(editor, "save", {"index": 1, "answers": answers[0]})[0] == 409


def test_edit_preserves_identity_confirmations_and_uncertainty(batch, monkeypatch):
    c, answers, path = batch
    old = json.loads(path.read_text())
    journal = Journal(batch_id(old))
    journal.set(1, "confirmed")
    journal.set(3, "uncertain")

    def review(config, rows, save, statuses):
        assert statuses() == ["confirmed", "ready", "uncertain"]
        save(2, {1: "B", 2: "Edited"})
        with pytest.raises(ValueError, match="cannot be edited"):
            save(1, rows[0])
        with pytest.raises(ValueError, match="cannot be edited"):
            save(3, rows[2])

    monkeypatch.setattr("gformbot.editor.review", review)
    config, rows, edited = cli._edit_batch(c, answers, path)
    assert edited == path and rows == [answers[0], {1: "B", 2: "Edited"}, answers[2]]
    assert json.loads(path.read_text())["batch_id"] == old["batch_id"]
    assert config._submitted_indices == [1] and journal.states() == {1: "confirmed", 3: "uncertain"}


def test_stale_sender_and_copied_plan_cannot_claim_after_edit(batch, monkeypatch, tmp_path):
    c, answers, path = batch
    original = json.loads(path.read_text())
    stale = Session(path, original, digest(path))
    copied = tmp_path / "copy.json"
    atomic_json(copied, original)
    other = Session(copied, original, digest(copied))
    monkeypatch.setattr("gformbot.editor.review", lambda c, rows, save, states: save(2, {1: "B", 2: "New"}))
    cli._edit_batch(c, answers, path)
    for session in (stale, other):
        with pytest.raises(ValueError, match="edited"):
            session.before_submit(1)
    fresh = Session(path, json.loads(path.read_text()), digest(path))
    fresh.before_submit(1)
    assert fresh.journal.states()[1] == "attempted"


def test_submission_claim_before_edit_protects_the_row(batch, monkeypatch):
    c, answers, path = batch
    session = Session(path, json.loads(path.read_text()), digest(path))
    session.before_submit(2)
    monkeypatch.setattr(
        "gformbot.editor.review", lambda c, rows, save, states: save(2, {1: "B", 2: "Changed"})
    )
    with pytest.raises(ValueError, match="cannot be edited"):
        cli._edit_batch(c, answers, path)
    assert cli._load_plan(path)[1] == answers


def test_interrupted_edit_fails_closed_and_stale_start_cannot_overwrite_it(batch):
    _, _, path = batch
    payload = json.loads(path.read_text())
    stale = Session(path, payload, digest(path))

    def interrupted_write(changed):
        atomic_json(path, changed)
        raise OSError("Interrupted before journal update")

    with pytest.raises(OSError, match="Interrupted"):
        stale.journal.edit(payload, 1, {1: "B", 2: "Keep edited data"}, interrupted_write)
    with pytest.raises(ValueError, match="edited after execution began"):
        Session(path, json.loads(path.read_text()), digest(path))
    with pytest.raises(ValueError, match="changed during execution"):
        stale.start()
    assert json.loads(path.read_text())["responses"][0]["2"] == "Keep edited data"


def test_editor_rejects_route_change_until_new_required_answers_are_supplied(tmp_path, monkeypatch):
    from test_routing import schema

    from gformbot.config import config_from_schema
    from gformbot.generator import validate_answer

    monkeypatch.chdir(tmp_path)
    c = config_from_schema(schema())
    original = validate_answer(c, {1: "Student", 2: "Math", 4: "Hi"})
    with running(c, [original]) as editor:
        status, data, _ = request(editor, "save", {"index": 1, "answers": {**original, 1: "Worker"}})
        assert status == 400 and data["question"] == 3 and 3 in data["active"]
        status, data, _ = request(
            editor, "save", {"index": 1, "answers": {**original, 1: "Worker", 3: "Teacher"}}
        )
        assert status == 200 and data["answers"]["3"] == "Teacher" and 2 not in data["active"]


def test_many_question_edit_roundtrips_journal_binding(batch, monkeypatch):
    c, _, _ = batch
    c.count = 1
    c.questions = [QuestionConfig(i, f"Question {i}", "text") for i in range(1, 13)]
    row = {i: "Original" for i in range(1, 13)}
    path = cli._save_plan(c, [row])
    monkeypatch.setattr(
        "gformbot.editor.review", lambda c, rows, save, states: save(1, {**row, 10: "Changed"})
    )
    cli._edit_batch(c, [row], path)
    session = Session(path, json.loads(path.read_text()), digest(path))
    session.before_submit(1)
    assert session.journal.states() == {1: "attempted"}


def test_compact_sample_and_final_menu_have_only_meaningful_actions(batch, monkeypatch):
    c, rows, _ = batch
    shown = []
    monkeypatch.setattr(cli, "_show_answer", lambda answer, config, index, **kw: shown.append((index, kw)))
    cli._pending_preview(c, rows)
    assert shown == [(1, {"sample": True})]
    menus = []
    monkeypatch.setattr(
        cli,
        "_ask_select",
        lambda message, choices, **kw: menus.extend(choice.name for choice in choices) or "back",
    )
    assert cli._next_action(3) == "back"
    assert menus == [
        "Submit 3 responses",
        "Review or edit responses",
        "Export remaining responses as CSV",
        "Back to start",
    ]
