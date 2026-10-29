import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event

import pytest

from gformbot.logger import log_result, validate_log_path


def test_appends_complete_answers_and_one_header(tmp_path):
    path = tmp_path / "nested" / "results.csv"
    answers = {1: "Café, A", 2: ["Roads, footpaths", "Bus stops"], 15: "", 20: None}
    log_result(path, 1, answers, True, "filled without submission")
    log_result(path, 2, {1: "A"}, False, "failure, details")

    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["status"] == "OK"
    assert rows[1]["status"] == "ERROR"
    assert rows[1]["message"] == "failure, details"
    assert json.loads(rows[0]["answers"]) == {str(key): value for key, value in answers.items()}
    assert datetime.fromisoformat(rows[0]["timestamp"]).utcoffset() == timedelta(0)


def test_empty_log_receives_header(tmp_path):
    path = tmp_path / "empty.csv"
    path.touch()
    log_result(path, 1, {}, True, "ok")
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["timestamp", "run", "status", "message", "answers"]
    assert len(rows) == 2


def test_preflight_does_not_create_a_log(tmp_path):
    path = tmp_path / "not-created" / "results.csv"
    validate_log_path(path)
    assert not path.parent.exists()


def test_legacy_log_rejected_before_execution_and_preserved(tmp_path):
    path = tmp_path / "legacy.csv"
    original = b"timestamp,run,status,message,q1,q2\r\nold,1,OK,done,A,B\r\n"
    path.write_bytes(original)
    with pytest.raises(ValueError, match="different format"):
        validate_log_path(path)
    with pytest.raises(ValueError, match="different format"):
        log_result(path, 2, {3: "new"}, True, "ok")
    assert path.read_bytes() == original


def test_directory_is_rejected_as_log(tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        validate_log_path(tmp_path)


def test_invalid_json_answer_does_not_create_partial_log(tmp_path):
    path = tmp_path / "not-created" / "results.csv"
    with pytest.raises(ValueError):
        log_result(path, 1, {1: float("nan")}, True, "ok")
    assert not path.parent.exists()


def test_concurrent_first_writes_preserve_one_header_and_complete_answers(tmp_path, monkeypatch):
    path = tmp_path / "shared.csv"
    append_window = Barrier(2, timeout=1)
    original = Path.open

    def overlapping_append(target, mode="r", *args, **kwargs):
        if target == path and mode == "a":
            try:
                append_window.wait()
            except BrokenBarrierError:
                pass  # A serialized writer waits here alone, then lets the next writer proceed.
        return original(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", overlapping_append)
    answers = {index: {1: f"Profile {index}: café 😛\n" * 2000} for index in (1, 2)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda index: log_result(path, index, answers[index], True, "confirmed", batch_id=str(index)),
                answers,
            )
        )
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    assert sum(row[0] == "timestamp" for row in rows) == 1
    assert len(rows) == 3
    assert {int(row[1]): json.loads(row[4]) for row in rows[1:]} == {
        index: {"1": answer[1]} for index, answer in answers.items()
    }


def test_preflight_waits_for_locked_partial_header(tmp_path):
    from gformbot.locking import file_lock

    path = tmp_path / "shared.csv"
    started = Event()

    def preflight():
        started.set()
        validate_log_path(path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(path):
            path.write_text("timestamp,ru", encoding="utf-8")
            result = pool.submit(preflight)
            assert started.wait(timeout=2)
            time.sleep(0.1)
            path.write_text("timestamp,run,status,message,answers\n", encoding="utf-8")
        result.result(timeout=3)


def test_identified_batch_appends_to_legacy_five_column_log(tmp_path):
    path = tmp_path / "legacy-compatible.csv"
    path.write_text("timestamp,run,status,message,answers\n", encoding="utf-8")
    log_result(path, 2, {1: "A"}, True, "confirmed", batch_id="batch-2", form_url="https://example.test")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 2 and all(len(row) == 5 for row in rows)
    assert rows[1][3] == "[batch=batch-2; form=https://example.test] confirmed"
    assert json.loads(rows[1][4]) == {"1": "A"}


def test_append_permission_failure_preserves_existing_bytes_and_releases_lock(tmp_path, monkeypatch):
    path = tmp_path / "results.csv"
    log_result(path, 1, {1: "First"}, True, "confirmed")
    original_bytes = path.read_bytes()
    original = Path.open

    def denied_append(target, mode="r", *args, **kwargs):
        if target == path and mode == "a":
            raise PermissionError("Read-only log")
        return original(target, mode, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", denied_append)
        with pytest.raises(PermissionError, match="Read-only log"):
            log_result(path, 2, {1: "Second"}, True, "confirmed")
    assert path.read_bytes() == original_bytes
    log_result(path, 3, {1: "Third"}, True, "confirmed")
    with path.open(encoding="utf-8", newline="") as stream:
        assert [row["run"] for row in csv.DictReader(stream)] == ["1", "3"]
