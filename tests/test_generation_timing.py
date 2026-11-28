import json

from gformbot import generation_timing, previews


def test_timing_reports_nested_repairs_without_counting_them_twice(monkeypatch, tmp_path):
    now = 0.0
    monkeypatch.setattr(generation_timing, "monotonic", lambda: now)
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "previews.json")
    timing = generation_timing.GenerationTiming()

    with timing.phase("review"):
        now = 1.0
        with timing.phase("repair"), timing.request("stream"):
            now = 3.0
        now = 4.0

    path = timing.save(completed=True)
    stats = json.loads(path.read_text(encoding="utf-8"))
    assert stats["wall_ms"] == 4000
    assert stats["phase_ms"] == {"repair": 2000, "review": 2000}
    assert [(request["phase"], request["total_ms"]) for request in stats["requests"]] == [("repair", 2000)]
