import json

import pytest

from gformbot.artifacts import cleanup, describe, digest
from gformbot.state import Journal, batch_id


def plan(tmp_path, *, managed=True, source=None):
    csv = tmp_path / "generated.csv"
    csv.write_text("Q1\nSynthetic\n")
    path = tmp_path / "plan.json"
    payload = {
        "kind": "gformbot-run",
        "schema_version": 1,
        "cleanup_on_success": managed,
        "source": "prepared",
        "config": {"pool_file": str(source) if source else None},
        "temporary_files": describe([csv], tmp_path),
        "responses": [{"1": "Synthetic"}],
        "submitted_indices": [1],
    }
    path.write_text(json.dumps(payload))
    Journal(batch_id(payload)).set(1, "confirmed")
    return path, csv, digest(path)


@pytest.mark.parametrize("policy", [None, "true", 1, {}, []])
def test_unknown_retention_policy_preserves_files(tmp_path, policy):
    path, csv, _ = plan(tmp_path)
    payload = json.loads(path.read_text())
    if policy is None:
        payload.pop("cleanup_on_success")
    else:
        payload["cleanup_on_success"] = policy
    path.write_text(json.dumps(payload))
    assert cleanup(path, digest(path)) == ([], [])
    assert path.exists() and csv.exists()


@pytest.mark.parametrize("source", [None, "command", "prepare", "unknown"])
def test_unknown_or_persistent_origin_preserves_files(tmp_path, source):
    path, csv, _ = plan(tmp_path)
    payload = json.loads(path.read_text())
    if source is None:
        payload.pop("source")
    else:
        payload["source"] = source
    path.write_text(json.dumps(payload))
    assert cleanup(path, digest(path)) == ([], [])
    assert path.exists() and csv.exists()


@pytest.mark.parametrize("state", ["prepared", "reviewing", "attempted", "uncertain"])
def test_cleanup_requires_all_rows_confirmed_in_journal(tmp_path, state):
    path, csv, _ = plan(tmp_path)
    payload = json.loads(path.read_text())
    payload["responses"].append({"1": "Unfinished"})
    payload["submitted_indices"] = [1, 2]  # Stale/edited progress is insufficient.
    path.write_text(json.dumps(payload))
    Journal(batch_id(payload)).set(2, state)
    removed, preserved = cleanup(path, digest(path))
    assert not removed and path in preserved
    assert path.exists() and csv.exists()


def test_success_removes_only_owned_outputs(tmp_path):
    path, csv, before = plan(tmp_path)
    journal = Journal(batch_id(json.loads(path.read_text())))
    unrelated = tmp_path / "gformbot-generated-other.csv"
    unrelated.write_text("Must stay")
    removed, preserved = cleanup(path, before)
    assert set(removed) == {path, csv}
    assert not preserved
    assert not path.exists() and not csv.exists()
    assert unrelated.read_text() == "Must stay"
    assert journal.path.exists() and journal.states() == {1: "confirmed"}


@pytest.mark.parametrize("changed", ["plan", "csv"])
def test_modified_files_preserved(tmp_path, changed):
    path, csv, before = plan(tmp_path)
    if changed == "plan":
        path.write_text(path.read_text() + "\n")
    else:
        csv.write_text("User edited this")
    removed, preserved = cleanup(path, before)
    assert not removed and path in preserved
    assert path.exists() and csv.exists()


def test_missing_csv_allows_plan_cleanup(tmp_path):
    path, csv, before = plan(tmp_path)
    csv.unlink()
    assert cleanup(path, before) == ([path], [])


def test_explicit_export_is_preserved(tmp_path):
    path, csv, before = plan(tmp_path, managed=False)
    assert cleanup(path, before) == ([], [])
    assert path.exists() and csv.exists()


def test_input_csv_is_never_removed_even_if_manifest_claims_it(tmp_path):
    path, csv, before = plan(tmp_path, source=tmp_path / "generated.csv")
    removed, preserved = cleanup(path, before)
    assert not removed and csv in preserved
    assert path.exists() and csv.exists()


@pytest.mark.parametrize("name", ["../outside.csv", "..\\outside.csv", "plan.json", ""])
def test_invalid_manifest_path_preserves_files(tmp_path, name):
    path, csv, before = plan(tmp_path)
    payload = json.loads(path.read_text())
    payload["temporary_files"][0]["name"] = name
    path.write_text(json.dumps(payload))
    removed, preserved = cleanup(path, digest(path))
    assert not removed and preserved
    assert path.exists() and csv.exists()


def test_unlink_failure_preserves_manifest(tmp_path, monkeypatch):
    path, csv, before = plan(tmp_path)
    original = type(path).unlink

    def unlink(p, *args, **kwargs):
        if p == csv:
            raise PermissionError("In use")
        return original(p, *args, **kwargs)

    monkeypatch.setattr(type(path), "unlink", unlink)
    removed, preserved = cleanup(path, before)
    assert not removed and csv in preserved and path in preserved


def test_outputs_in_other_directories_cannot_be_claimed(tmp_path):
    csv = tmp_path / "nested" / "input.csv"
    csv.parent.mkdir()
    csv.write_text("Not a sibling output")
    with pytest.raises(ValueError, match="share an output directory"):
        describe([csv], tmp_path)
