"""Concurrent saves preserve each prepared batch and its preview index entry."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local

from gformbot import cli, inspector, previews
from gformbot.config import QuestionConfig, RunConfig


def test_preview_index_remembers_concurrent_paths(tmp_path):
    paths = [tmp_path / f"plan-{index}.json" for index in range(30)]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(previews.remember, paths))
    assert set(previews._remembered()) == {path.resolve() for path in paths}
    assert not list(previews.index_path().parent.glob("*.tmp"))


def concurrent_plans(tmp_path, monkeypatch, *, explicit):
    monkeypatch.chdir(tmp_path)
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[QuestionConfig(1, "Name", "text", True)],
    )
    fingerprint = inspector.schema_fingerprint
    ready, first_finished, writer = Barrier(2), Event(), local()

    def synchronized_fingerprint(value):
        # Both calls have passed initial filename selection before either saves.
        # No waiting is injected inside the save's locked critical section.
        result = fingerprint(value)
        ready.wait(timeout=5)
        if writer.name == "second":
            assert first_finished.wait(timeout=5)
        return result

    monkeypatch.setattr(inspector, "schema_fingerprint", synchronized_fingerprint)

    def save(name):
        writer.name = name
        try:
            return cli._save_plan(config, [{1: name}], tmp_path / "shared.json" if explicit else None)
        except ValueError as exc:
            return exc
        finally:
            if name == "first":
                first_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(save, ["first", "second"]))


def test_explicit_plan_collision_cannot_overwrite_a_completed_save(tmp_path, monkeypatch):
    first, second = concurrent_plans(tmp_path, monkeypatch, explicit=True)
    assert first == tmp_path / "shared.json"
    assert isinstance(second, ValueError) and "already exists" in str(second)
    assert json.loads(first.read_text())["responses"] == [{"1": "first"}]


def test_automatic_concurrent_plans_get_distinct_names_and_identities(tmp_path, monkeypatch):
    paths = concurrent_plans(tmp_path, monkeypatch, explicit=False)
    assert all(not isinstance(path, Exception) for path in paths)
    assert len(set(paths)) == 2
    payloads = [json.loads(path.read_text()) for path in paths]
    assert [payload["responses"] for payload in payloads] == [[{"1": "first"}], [{"1": "second"}]]
    assert len({payload["batch_id"] for payload in payloads}) == 2
    assert set(previews._remembered()) == {path.resolve() for path in paths}
