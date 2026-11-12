"""Question partitions preserve routes, identities and complete-profile failure boundaries."""

import json
from copy import deepcopy
from threading import Barrier, current_thread

import pytest

from gformbot import ai, ai_partition, chatgpt_http
from gformbot.ai_partition import generate_partitioned
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def config():
    return RunConfig(
        "https://docs.google.com/forms/d/e/partition/viewform",
        sections=["Start", "Left", "Right", "End"],
        section_next=[2, 4, 4, 0],
        questions=[
            QuestionConfig(1, "Route", "radio", True, ["Left", "Right"], branches={"Left": 2, "Right": 3}),
            QuestionConfig(2, "Left answer", "text", True, page=2),
            QuestionConfig(
                3,
                "Grid [A]",
                "radio",
                True,
                ["One", "Two"],
                page=3,
                item_id="grid",
                entry_id="a",
                grid_title="Grid",
                grid_row="A",
                grid_unique=True,
            ),
            QuestionConfig(
                4,
                "Grid [B]",
                "radio",
                True,
                ["One", "Two"],
                page=3,
                item_id="grid",
                entry_id="b",
                grid_title="Grid",
                grid_row="B",
                grid_unique=True,
            ),
            QuestionConfig(5, "Closing answer", "text", True, page=4),
        ],
    )


def fake_generator(monkeypatch, *, fail_right=False):
    calls = []

    def generate(config, count, **options):
        calls.append((config, options))
        assert options["_partition"] is False
        if "Routing metadata:" in config.form_description:
            return [{1: "Left" if i == 0 else "Right"} for i in range(count)]
        if fail_right and any(q.index == 3 for q in config.questions):
            raise ai.GenerationError("Connection interrupted", partial_responses=[{3: "One"}])
        output = []
        for offset, preset in enumerate(options["partials"]):
            identity = options["_profile_context"][offset]["response_number"]
            values = {
                1: "Left" if identity == 1 else "Right",
                2: "Left generated",
                3: "One",
                4: "Two",
                5: f"End {identity}",
            }
            output.append({q.index: preset.get(q.index, values[q.index]) for q in config.questions})
        return output

    monkeypatch.setattr(ai, "_generate_responses", generate)
    return calls


def test_partitions_follow_routes_preserve_presets_and_keep_grid_rows_together(config, monkeypatch):
    calls = fake_generator(monkeypatch)
    rows = generate_partitioned(config, 2, partials=[{2: "Imported unchanged"}, {}])

    assert rows == [
        {1: "Left", 2: "Imported unchanged", 3: "", 4: "", 5: "End 1"},
        {1: "Right", 2: "", 3: "One", 4: "Two", 5: "End 2"},
    ]
    for subconfig, _ in calls:
        keys = {q.index for q in subconfig.questions}
        assert bool(keys & {3, 4}) == ({3, 4} <= keys)
        assert all(q.page == 1 and not q.branches for q in subconfig.questions)
    assert all(options["_client"] is calls[0][1]["_client"] for _, options in calls)
    assert config.questions[0].branches == {"Left": 2, "Right": 3}


def test_partition_failure_exposes_only_complete_original_profiles(config, monkeypatch):
    fake_generator(monkeypatch, fail_right=True)
    progress = []
    with pytest.raises(ai.GenerationError, match="Connection interrupted") as failure:
        generate_partitioned(config, 2, progress=lambda *args: progress.append(args))
    assert failure.value.completed == 1
    assert failure.value.requested == 2
    assert failure.value.partial_responses == [{1: "Left", 2: "Left generated", 3: "", 4: "", 5: "End 1"}]
    assert progress == [(1, 2)]


def test_branches_rejoin_before_generating_shared_tail(config, monkeypatch):
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    calls = fake_generator(monkeypatch)
    rows = generate_partitioned(config, 2)
    tails = [(c.count, options) for c, options in calls if [q.index for q in c.questions] == [5]]
    assert len(tails) == 1 and tails[0][0] == 2
    assert [row[5] for row in rows] == ["End 1", "End 2"]


def test_join_scheduler_handles_cyclic_union_of_terminating_routes(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/joins/viewform",
        sections=["Start", "A", "B", "End"],
        section_next=[2, 4, 4, 0],
        questions=[
            QuestionConfig(1, "Start", "radio", True, ["A", "B"], branches={"A": 2, "B": 3}),
            QuestionConfig(2, "A", "radio", True, ["B", "End"], page=2, branches={"B": 3, "End": 4}),
            QuestionConfig(3, "B", "radio", True, ["A", "End"], page=3, branches={"A": 2, "End": 4}),
            QuestionConfig(4, "Closing", "text", True, page=4),
        ],
    )
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    profiles = [{1: "A", 2: "B", 3: "End", 4: "First"}, {1: "B", 2: "End", 3: "A", 4: "Second"}]
    calls = []

    def generate(subconfig, count, **options):
        calls.append(True)
        assert len(calls) <= 8, "Join scheduling stopped making progress"
        return [
            {q.index: profiles[context["response_number"] - 1][q.index] for q in subconfig.questions}
            for context in options["_profile_context"]
        ]

    monkeypatch.setattr(ai, "_generate_responses", generate)
    assert generate_partitioned(config, 2) == profiles


def test_routing_loops_receive_bounded_corrections_before_any_question_group(config, monkeypatch):
    config.questions[0].branches["Left"] = 1
    calls = []

    def generate(subconfig, count, **options):
        calls.append(subconfig)
        assert "Routing metadata:" in subconfig.form_description
        return [{1: "Left"}]

    monkeypatch.setattr(ai, "_generate_responses", generate)
    with pytest.raises(ai.GenerationError, match="supported terminating routes"):
        generate_partitioned(config, 1, max_retries=1)
    assert len(calls) == 2


def test_real_generation_validates_partitioned_profiles_with_local_ids_and_duplicate_titles(
    config, monkeypatch
):
    config.questions[1].text = config.questions[-1].text = "Comment"
    config.questions[-1].fixed_value = "Final fixed"
    config.questions.insert(
        2,
        QuestionConfig(6, "Left navigation", "radio", True, ["Finish"], page=2, branches={"Finish": 4}),
    )
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    calls = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        records = []
        for local_id, preset in zip(task["profile_numbers"], task["partial_profiles"], strict=True):
            context = task.get("profile_context", {}).get(str(local_id), {})
            global_id = context.get("response_number", local_id)
            values = {
                "1": 1 if global_id == 1 else 2,
                "2": "Left generated",
                "3": 1,
                "4": 2,
                "5": "Final fixed",
                "6": 1,
            }
            answer = {
                question["key"]: preset.get(question["key"], values[question["key"]])
                for question in task["questions"]
            }
            records.append(json.dumps({"profile_id": local_id, "answers": answer}))
        return "\n".join(records)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    rows = generate_partitioned(config, 2, partials=[{2: "Preserved"}, {}])

    assert rows == [
        {1: "Left", 2: "Preserved", 3: "", 4: "", 5: "Final fixed", 6: "Finish"},
        {1: "Right", 2: "", 3: "One", 4: "Two", 5: "Final fixed", 6: ""},
    ]
    assert any(task.get("profile_context", {}).get("1", {}).get("response_number") == 2 for task in calls)
    shared_labels = [
        {key: title for key, title, values in task["shared_context"]["history"]["columns"]}
        for task in calls
        if "shared_context" in task
    ]
    duplicate_labels = [labels for labels in shared_labels if {"2", "5"} <= labels.keys()]
    assert duplicate_labels and all(labels["2"] == labels["5"] == "Comment" for labels in duplicate_labels)
    routing = json.loads(calls[0]["form_description"].split("Routing metadata: ", 1)[1])
    assert routing["question_sections"] == {"1": 1, "6": 2}
    assert routing["section_titles"] == config.sections
    final_context = next(
        task["shared_context"]
        for task in calls
        if task.get("shared_context", {}).get("original_question_sections") == {"5": 4}
    )
    assert final_context["route_section_titles"]["4"] == config.sections[3]


def test_large_profile_groups_use_two_workers_without_mixing_presets_or_context(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/parallel/viewform",
        questions=[QuestionConfig(7, "Written answer", "text", True, description="x" * 25_000)],
    )
    barrier = Barrier(2)
    calls = []

    def generate(subconfig, count, **options):
        barrier.wait(timeout=3)
        identities = [profile["response_number"] for profile in options["_profile_context"]]
        assert current_thread().name.startswith("gformbot-ai")
        assert count == subconfig.count == len(identities)
        assert options["partials"] == [{7: f"original-{identity}"} for identity in identities]
        calls.append((identities, options["_client"]))
        return options["partials"]

    monkeypatch.setattr(ai, "_generate_responses", generate)
    originals = [{7: f"original-{identity}"} for identity in range(1, 7)]
    assert generate_partitioned(config, 6, partials=originals) == originals
    assert sorted(identities for identities, _ in calls) == [[1, 2, 3], [4, 5, 6]]
    assert calls[0][1] is calls[1][1]


def test_profiles_that_fit_share_one_request_instead_of_two(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/fit/viewform",
        questions=[QuestionConfig(7, "Written answer", "text", True)],
    )
    calls = []

    def generate(subconfig, count, **options):
        calls.append(count)
        return options["partials"]

    monkeypatch.setattr(ai, "_generate_responses", generate)
    originals = [{7: f"original-{i}"} for i in range(6)]
    assert generate_partitioned(config, 6, partials=originals) == originals
    assert calls == [6]


def test_parallel_failure_keeps_successful_worker_and_noncontiguous_partial_ids(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/recovery/viewform",
        questions=[QuestionConfig(7, "Written answer", "text", True, description="x" * 25_000)],
    )
    barrier = Barrier(2)

    def generate(subconfig, count, **options):
        barrier.wait(timeout=3)
        identities = [profile["response_number"] for profile in options["_profile_context"]]
        if identities == [1, 2, 3]:
            return options["partials"]
        failure = ai.GenerationError("Request limited (HTTP 429)")
        # The failed worker completed local rows 3 and 1; row 2 is still missing.
        failure.partial_records = {2: options["partials"][2], 0: options["partials"][0]}
        raise failure

    monkeypatch.setattr(ai, "_generate_responses", generate)
    originals = [{7: f"original-{i}"} for i in range(1, 7)]
    progress = []
    with pytest.raises(ai.GenerationError, match="HTTP 429") as caught:
        generate_partitioned(config, 6, partials=originals, progress=lambda *v: progress.append(v))

    failure = caught.value
    assert failure.completed == 5
    assert failure.partial_records == {i: originals[i] for i in (0, 1, 2, 3, 5)}
    assert failure.partial_responses == [originals[i] for i in (0, 1, 2, 3, 5)]
    assert failure.partial_groups == {4: {"answers": originals[4], "position": 0, "route": (1,)}}
    assert [done for done, _ in progress] == list(range(1, 6))


def test_unfinished_groups_are_retained_without_becoming_complete_profiles(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/groups/viewform",
        questions=[
            QuestionConfig(3, "First", "text", True),
            QuestionConfig(8, "Still missing", "text", True),
        ],
    )
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)

    def generate(subconfig, count, **options):
        if subconfig.questions[0].index == 3:
            return [{3: "Keep this validated group"}]
        raise ai.GenerationError("Request limited (HTTP 429)")

    monkeypatch.setattr(ai, "_generate_responses", generate)
    with pytest.raises(ai.GenerationError, match="HTTP 429") as caught:
        generate_partitioned(config, 1)

    failure = caught.value
    assert failure.completed == 0 and failure.partial_records == {} and failure.partial_responses == []
    assert failure.partial_groups == {
        0: {"answers": {3: "Keep this validated group"}, "position": 1, "route": (1,)}
    }


def test_checkpoint_resumes_only_missing_groups_and_preserves_completed_rows(config, monkeypatch):
    snapshots = []
    fake_generator(monkeypatch, fail_right=True)
    with pytest.raises(ai.GenerationError, match="Connection interrupted"):
        generate_partitioned(config, 2, checkpoint=lambda state: snapshots.append(deepcopy(state)))
    saved = snapshots[-1]
    assert saved["profiles"][0]["route"] == [1, 2, 4]
    assert saved["profiles"][1]["route"] == [1, 3, 4]
    calls = fake_generator(monkeypatch)
    output = generate_partitioned(config, 2, resume_state=saved)
    assert output == [
        {1: "Left", 2: "Left generated", 3: "", 4: "", 5: "End 1"},
        {1: "Right", 2: "", 3: "One", 4: "Two", 5: "End 2"},
    ]
    assert all("Routing metadata:" not in subconfig.form_description for subconfig, _ in calls)
    assert all(
        profile["response_number"] == 2 for _, options in calls for profile in options["_profile_context"]
    )


@pytest.mark.parametrize(
    "change", ["missing_prefix", "future_answer", "changed_route", "split_grid", "preset"]
)
def test_invalid_draft_progress_fails_before_generation(config, monkeypatch, change):
    record = {"answers": {"1": "Right", "3": "One", "4": "Two"}, "position": 2, "route": [1, 3, 4]}
    partials = None
    if change == "missing_prefix":
        record["answers"].pop("3")
    elif change == "future_answer":
        record["answers"]["5"] = "Not completed"
    elif change == "changed_route":
        record["route"] = [1, 2, 4]
    elif change == "split_grid":
        record["answers"].pop("4")
    else:
        partials = [{1: "Left"}]
    monkeypatch.setattr(
        ai, "_generate_responses", lambda *args, **kwargs: pytest.fail("Invalid draft generated")
    )
    with pytest.raises(ValueError, match="AI draft|draft"):
        generate_partitioned(config, 1, resume_state={"profiles": [record]}, partials=partials)


def test_validated_progress_is_checkpointed_on_interrupt(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/interrupted/viewform",
        questions=[QuestionConfig(3, "First", "text", True), QuestionConfig(8, "Last", "text", True)],
    )
    monkeypatch.setattr(ai_partition, "_GROUP_BYTES", 1)
    snapshots = []

    def interrupted(subconfig, count, **options):
        if subconfig.questions[0].index == 3:
            return [{3: "Retained"}]
        raise KeyboardInterrupt

    monkeypatch.setattr(ai, "_generate_responses", interrupted)
    with pytest.raises(KeyboardInterrupt):
        generate_partitioned(config, 1, checkpoint=lambda state: snapshots.append(deepcopy(state)))
    assert snapshots[-1]["profiles"] == [{"answers": {"3": "Retained"}, "position": 1, "route": [1]}]


def test_completed_draft_returns_without_generation(config, monkeypatch):
    calls = fake_generator(monkeypatch)
    snapshots = []
    expected = generate_partitioned(config, 2, checkpoint=lambda state: snapshots.append(deepcopy(state)))
    calls.clear()
    assert generate_partitioned(config, 2, resume_state=snapshots[-1]) == expected
    assert calls == []


def test_restored_history_preserves_acceptance_order_after_json_key_sorting(monkeypatch):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/ordered/viewform",
        questions=[QuestionConfig(index, f"Answer {index}", "text", True) for index in range(1, 22)],
    )
    state = {
        "profiles": [
            {
                "answers": {str(q.index): f"Value {q.index}" for q in config.questions},
                "position": len(config.questions),
                "route": [1],
            }
        ]
    }
    state = json.loads(json.dumps(state, sort_keys=True))
    snapshots = []
    monkeypatch.setattr(
        ai, "_generate_responses", lambda *args, **kwargs: pytest.fail("Completed draft generated")
    )
    generate_partitioned(config, 1, resume_state=state, checkpoint=snapshots.append)
    assert list(snapshots[-1]["profiles"][0]["answers"]) == [str(index) for index in range(1, 22)]
