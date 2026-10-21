import csv
import json
from datetime import datetime, timedelta

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
