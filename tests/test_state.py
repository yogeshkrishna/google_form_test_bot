import json
import sqlite3
from contextlib import closing

import pytest

from gformbot import cli, logger, transport
from gformbot.access import SubmissionRejectedError, SubmissionUncertainError
from gformbot.artifacts import digest
from gformbot.config import QuestionConfig, RunConfig
from gformbot.state import Journal, Session, UncertainSubmission, batch_id


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        count=2,
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1", item_id="1")],
        min_delay=0,
        max_delay=0,
        log_file=str(tmp_path / "runs.csv"),
    )
    answers = [{1: "First"}, {1: "Second"}]
    path = cli._save_plan(c, answers)
    return c, answers, path


def session(saved):
    c, answers, path = saved
    return Session(path, json.loads(path.read_text()), digest(path))


def test_start_is_persisted_once_without_changing_identity_or_response_states(saved):
    current = session(saved)
    identity, ids = current.identity, current.ids.copy()
    current.start()
    first = current.path.read_bytes()
    current.start()
    assert current.path.read_bytes() == first
    assert current.payload["submission_started"] is True
    assert current.identity == identity and current.ids == ids
    assert current.journal.states() == {}
    matches, _ = cli._saved_batches()
    assert len(matches) == 1


def test_connection_failure_before_first_submission_keeps_batch_discoverable(saved, monkeypatch):
    c, answers, path = setup_runner(saved, monkeypatch)

    def disconnected(*args, **kwargs):
        raise RuntimeError("Network unavailable before first response")

    monkeypatch.setattr(transport, "Client", disconnected)
    with pytest.raises(RuntimeError, match="Network unavailable"):
        cli._run_submit(c, answers=answers)
    restored, rows = cli._load_plan(path)
    assert restored._submission_started and restored._submitted_indices == []
    assert rows == answers and restored._outcomes == {}
    assert len(cli._saved_batches()[0]) == 1


def setup_runner(saved, monkeypatch):
    c, answers, path = saved
    cli._track_plan(c, answers, path, digest(path))
    monkeypatch.setattr(cli, "interactive_input", lambda: False)

    class Client:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, answer, **options):
            pytest.fail("Each execution test must supply its HTTP outcome")

    monkeypatch.setattr(transport, "Client", Client)
    return c, answers, path


@pytest.mark.parametrize(("requested", "confirmed"), [(2, 0), (3, 0), (3, 1)])
def test_submission_summary_keeps_requested_and_confirmed_totals(
    saved, monkeypatch, capsys, requested, confirmed
):
    config, answers, path = saved
    path = cli._save_plan(config, answers, path.with_name("partial.json"))
    if requested > config.count:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["requested_count"] = requested
        path.write_text(json.dumps(payload), encoding="utf-8")
    previous = session((config, answers, path))
    if confirmed:
        previous.start()
        previous.before_submit(1)
        previous.confirm(1)
    config, answers = cli._load_plan(path)
    config, _, _ = setup_runner((config, answers, path), monkeypatch)

    sent = []

    def submit(self, answer, *, before_submit, **kwargs):
        before_submit()
        sent.append(answer)
        return True, "Accepted"

    monkeypatch.setattr(transport.Client, "submit", submit)
    assert cli._run_submit(config, answers=answers[confirmed:])
    assert sent == answers[confirmed:]
    expected = (
        "Finished: 2/2 submitted"
        if requested == 2
        else "Finished: 2 of 3 submitted · 1 skipped during generation"
    )
    assert expected in capsys.readouterr().out


def test_uncertain_submit_is_not_silently_requeued(saved, monkeypatch):
    c, answers, path = setup_runner(saved, monkeypatch)

    def uncertain(self, answer, *, before_submit, **kwargs):
        before_submit()
        raise SubmissionUncertainError("No acceptance appeared")

    monkeypatch.setattr(transport.Client, "submit", uncertain)
    with pytest.raises(ValueError, match="uncertain"):
        cli._run_submit(c, answers=answers)
    restored, rows = cli._load_plan(path)
    with pytest.raises(UncertainSubmission, match="previous outcome"):
        cli._track_plan(restored, rows, path, digest(path))


@pytest.mark.parametrize("interrupted", [False, True])
def test_submission_persists_scoped_form_notes_in_batches(saved, monkeypatch, interrupted):
    c, answers, path = setup_runner(saved, monkeypatch)
    journal = c._session.journal
    writes = []
    record = Journal.record_followups

    def track(self, response, observations, log_path, title):
        writes.append((response, [(section, stage) for section, stage, _ in observations]))
        record(self, response, observations, log_path, title)

    monkeypatch.setattr(Journal, "record_followups", track)

    def submit(self, answer, *, before_submit, observe):
        response = answers.index(answer) + 1
        observe(0, "intro", [{"text": "Introduction"}])
        observe(
            1,
            "section",
            [{"text": "One"}, {"text": "Visit https://example.test/help?token=private."}],
        )
        observe(3, "section", [{"text": "Three"}])
        if interrupted:
            raise KeyboardInterrupt
        before_submit()
        assert journal.states()[response] == "attempted"
        records = [item for item in journal.followups() if item["response"] == response]
        assert [item["content"]["text"] for item in records] == [
            "Introduction",
            "One",
            "Visit https://example.test/help?token=REDACTED.",
            "Three",
        ]
        observe(0, "confirmation", [{"text": "Received"}])
        assert journal.followups()[-1]["content"]["text"] == "Received"
        return True, "Accepted"

    monkeypatch.setattr(transport.Client, "submit", submit)
    prep = [(0, "intro"), (1, "section"), (1, "section"), (3, "section")]
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            cli._run_submit(c, answers=answers)
        assert writes == [(1, prep)]
        assert journal.states() == {}
        assert [item["section"] for item in journal.followups()] == [0, 1, 1, 3]
    else:
        assert cli._run_submit(c, answers=answers)
        assert writes == [(1, prep), (1, [(0, "confirmation")]), (2, prep), (2, [(0, "confirmation")])]
        assert journal.states() == {1: "confirmed", 2: "confirmed"}
    assert path.is_file()


def test_missing_retention_policy_preserves_identity_progress_and_uncertainty(saved):
    from gformbot.artifacts import cleanup

    _, _, path = saved
    payload = json.loads(path.read_text())
    payload.pop("cleanup_on_success")
    path.write_text(json.dumps(payload))
    identity, fingerprint = batch_id(payload), payload["form_fingerprint"]
    current = Session(path, payload, digest(path))
    current.before_submit(1)
    current.confirm(1)
    current.before_submit(2)
    for _ in range(2):
        before = path.read_bytes()
        restored, rows = cli._load_plan(path)
        assert restored._submitted_indices == [1]
        with pytest.raises(UncertainSubmission):
            cli._track_plan(restored, rows, path, digest(path), interactive=False)
        assert path.read_bytes() == before
        assert cleanup(path, digest(path)) == ([], [])
    final = json.loads(path.read_text())
    assert (final["batch_id"], final["form_fingerprint"]) == (identity, fingerprint)
    assert current.journal.states() == {1: "confirmed", 2: "attempted"}


def test_log_failure_after_success_keeps_confirmed_progress(saved, monkeypatch):
    c, answers, path = setup_runner(saved, monkeypatch)

    def accepted(self, answer, *, before_submit, **kwargs):
        before_submit()
        return True, "submitted"

    monkeypatch.setattr(transport.Client, "submit", accepted)
    monkeypatch.setattr(logger, "log_result", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ValueError, match="disk full"):
        cli._run_submit(c, answers=answers)
    restored, rows = cli._load_plan(path)
    assert restored._submitted_indices == [1]
    assert cli._track_plan(restored, rows, path, digest(path)) == [answers[1]]
    assert restored._session.ids == [2]


def test_copied_stale_plan_uses_shared_confirmations(saved):
    c, answers, path = saved
    copied = path.with_name("copy.json")
    copied.write_bytes(path.read_bytes())
    current = session(saved)
    current.before_submit(1)
    current.confirm(1)
    restored, rows = cli._load_plan(copied)
    assert restored._submitted_indices == [1]
    assert cli._track_plan(restored, rows, copied, digest(copied)) == [answers[1]]


def test_journal_failure_prevents_submit(saved, monkeypatch):
    c, answers, path = setup_runner(saved, monkeypatch)
    clicked = []

    def failing_write(*args, **kwargs):
        raise ValueError("journal unavailable")

    monkeypatch.setattr(Journal, "set", failing_write)

    def execute(self, answer, *, before_submit, **kwargs):
        before_submit()
        clicked.append(True)
        return True, "submitted"

    monkeypatch.setattr(transport.Client, "submit", execute)
    with pytest.raises(ValueError, match="journal unavailable"):
        cli._run_submit(c, answers=answers)
    assert clicked == []


def test_reconciliation_is_explicit_and_original_ids_survive(saved, monkeypatch):
    c, answers, path = saved
    current = session(saved)
    current.before_submit(1)
    monkeypatch.setattr(cli, "interactive_input", lambda: True)
    monkeypatch.setattr(cli, "_ask_select", lambda *a, **kw: "yes")
    restored, rows = cli._load_plan(path)
    assert cli._track_plan(restored, rows, path, digest(path)) == [answers[1]]
    assert restored._session.ids == [2]
    restored._session.before_submit(1)
    restored._session.confirm(1)
    assert json.loads(path.read_text())["submitted_indices"] == [1, 2]


def test_editing_executed_batch_requires_new_identity(saved):
    c, answers, path = saved
    current = session(saved)
    current.before_submit(1)
    payload = json.loads(path.read_text())
    payload["responses"][0]["1"] = "Edited"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="edited after execution"):
        Session(path, payload, digest(path))


def test_second_session_cannot_repeat_an_active_attempt(saved):
    first = session(saved)
    second = session(saved)
    first.before_submit(1)
    with pytest.raises(UncertainSubmission):
        second.before_submit(1)


def test_legacy_confirmations_are_shared_with_stale_copies(saved):
    c, answers, path = saved
    payload = json.loads(path.read_text())
    payload.pop("batch_id")
    copied = path.with_name("legacy-copy.json")
    copied.write_text(json.dumps(payload))
    payload["submitted_indices"] = [1]
    path.write_text(json.dumps(payload))
    current = Session(path, payload, digest(path))
    assert current.journal.states() == {1: "confirmed"}
    restored, rows = cli._load_plan(copied)
    assert restored._submitted_indices == [1]
    assert cli._track_plan(restored, rows, copied, digest(copied)) == [answers[1]]


def test_plan_write_failure_after_confirmation_still_skips_submitted_response(saved, monkeypatch):
    from gformbot import state

    c, answers, path = saved
    current = session(saved)
    current.before_submit(1)

    def fail_write(*args):
        raise OSError("plan disk full")

    monkeypatch.setattr(state, "atomic_json", fail_write)
    with pytest.raises(OSError, match="plan disk full"):
        current.confirm(1)
    assert current.journal.states() == {1: "confirmed"}
    assert json.loads(path.read_text()).get("submitted_indices", []) == []
    restored, rows = cli._load_plan(path)
    assert restored._submitted_indices == [1]
    assert cli._track_plan(restored, rows, path, digest(path)) == [answers[1]]


def test_stale_execution_cannot_erase_a_confirmed_outcome(saved):
    stale = session(saved)
    current = session(saved)
    current.before_submit(1)
    current.confirm(1)
    with pytest.raises(UncertainSubmission, match="confirmed"):
        stale.failed(1, uncertain=False)
    with pytest.raises(UncertainSubmission, match="confirmed"):
        stale.failed(1, uncertain=True)
    assert current.journal.states() == {1: "confirmed"}


def test_definitive_submit_rejection_does_not_require_uncertain_reconciliation(saved, monkeypatch):
    c, answers, path = setup_runner(saved, monkeypatch)

    def rejected(self, answer, *, before_submit, **kwargs):
        before_submit()
        raise SubmissionRejectedError("Form rejected the response: invalid answer")

    monkeypatch.setattr(transport.Client, "submit", rejected)
    with pytest.raises(ValueError, match="Form rejected"):
        cli._run_submit(c, answers=answers)
    assert c._session.journal.states() == {1: "prepared"}
    restored, rows = cli._load_plan(path)
    assert cli._track_plan(restored, rows, path, digest(path)) == answers


def test_simultaneous_sessions_allow_only_one_submit_intent(saved):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    current = session(saved)
    barrier = Barrier(4)

    def attempt(_):
        journal = Journal(current.identity)
        barrier.wait(timeout=5)
        try:
            journal.set(1, "attempted")
        except UncertainSubmission:
            return "blocked"
        return "accepted"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sorted(results) == ["accepted", "blocked", "blocked", "blocked"]
    assert current.journal.states() == {1: "attempted"}


def test_legacy_interrupted_review_remains_uncertain_on_reopen(saved):
    current = session(saved)
    current.journal.set(1, "reviewing")
    reopened = session(saved)
    assert reopened.uncertain() == [1]
    with pytest.raises(UncertainSubmission, match="reviewing"):
        reopened.before_submit(1)


def test_legacy_journal_reviews_still_require_reconciliation(saved):
    from gformbot.state import batch_id

    path = saved[2]
    payload = json.loads(path.read_text())
    journal = Journal(batch_id(payload))
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(journal.path)) as connection, connection:
        connection.execute(
            "CREATE TABLE responses (batch TEXT NOT NULL, response INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(batch,response))"
        )
        connection.execute("CREATE TABLE batches (batch TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO responses VALUES(?,?,?)", (journal.identity, 1, "reviewing"))
    current = session(saved)
    assert current.uncertain() == [1]
    with pytest.raises(UncertainSubmission):
        current.before_submit(1)
    current.reconcile(1, False)
    current.before_submit(1)
    assert current.journal.states() == {1: "attempted"}


def test_retired_upload_data_does_not_change_submission_recovery(saved):
    journal = Journal(batch_id(json.loads(saved[2].read_text())))
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(journal.path)) as connection, connection:
        connection.execute(
            "CREATE TABLE responses(batch TEXT,response INTEGER,state TEXT,PRIMARY KEY(batch,response))"
        )
        connection.executemany(
            "INSERT INTO responses VALUES(?,?,?)",
            [(journal.identity, 1, "confirmed"), (journal.identity, 2, "attempted")],
        )
        connection.execute(
            "CREATE TABLE uploads(batch TEXT,response INTEGER,question INTEGER,manifest TEXT,state TEXT,ids TEXT)"
        )
        upload = (journal.identity, 2, 3, "legacy-manifest", "uploading", '["legacy-file-id"]')
        connection.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?)", upload)
    for _ in range(2):
        current = session(saved)
        assert current.confirmed == {1}
        assert current.ids == [2]
        assert current.uncertain() == [2]
        with pytest.raises(UncertainSubmission):
            current.before_submit(1)
        assert current.journal.states() == {1: "confirmed", 2: "attempted"}
    with closing(sqlite3.connect(journal.path)) as connection:
        assert connection.execute("SELECT * FROM uploads").fetchall() == [upload]
    assert not hasattr(Journal, "begin_upload")
