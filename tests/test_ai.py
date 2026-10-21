"""The browser adapter preserves complete profiles and fails explicitly."""

import json

import pytest

from gformbot import ai
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def config(monkeypatch):
    class Driver:
        def quit(self):
            pass

    monkeypatch.setattr(ai, "_open_chat", lambda browser: Driver())
    return RunConfig(
        url="https://docs.google.com/forms/d/e/test/viewform",
        form_title="Example survey",
        questions=[
            QuestionConfig(index=1, text="Choice", qtype="radio", required=True, options=["A", "B"]),
            QuestionConfig(
                index=2,
                text="Activities",
                qtype="checkbox",
                required=True,
                options=["Read, write", "Run"],
                max_select=2,
            ),
            QuestionConfig(index=3, text="Reason", qtype="text", required=True),
            QuestionConfig(index=4, text="Comments", qtype="textarea"),
        ],
    )


def profile(choice="A", text="Useful for testing"):
    return {"1": choice, "2": ["Read, write"], "3": text, "4": ""}


def reply(*rows):
    return json.dumps({"responses": list(rows)})


def mock_chat(monkeypatch, contents):
    calls = []
    iterator = iter(contents)

    def chat(driver, messages, schema, timeout, **kwargs):
        calls.append({"schema": schema, "task": json.loads(messages[-1]["content"])})
        return next(iterator)

    monkeypatch.setattr(ai, "_chat", chat)
    return calls


def test_batches_complete_profiles_and_progress(config, monkeypatch):
    calls = mock_chat(monkeypatch, [reply(profile(), profile("B")), reply(profile())])
    progress = []
    rows = ai.generate_responses(
        config, 3, batch_size=2, progress=lambda done, total: progress.append((done, total))
    )
    assert len(rows) == 3
    assert all(set(row) == {1, 2, 3, 4} for row in rows)
    assert rows[0][2] == ["Read, write"]
    assert rows[0][4] == ""
    assert [call["schema"]["properties"]["responses"]["maxItems"] for call in calls] == [2, 1]
    assert progress == [(2, 3), (3, 3)]


def test_retains_valid_profiles_when_correcting_bad_ones(config, monkeypatch):
    bad = profile("not an option")
    calls = mock_chat(monkeypatch, [reply(profile(), bad), reply(profile("B"))])
    rows = ai.generate_responses(config, 2)
    assert [row[1] for row in rows] == ["A", "B"]
    assert calls[1]["task"]["response_count"] == 1
    assert "corrections" in calls[1]["task"]


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        '{"responses": []}',
        '{"responses": [{"1": "A"}]}',
        reply({**profile(), "unexpected": "value"}),
        reply({**profile(), "2": ["Run", "Run"]}),
        reply({**profile(), "3": ""}),
    ],
)
def test_bounded_corrections_and_no_random_fallback(config, monkeypatch, bad):
    calls = mock_chat(monkeypatch, [bad, bad, bad])
    with pytest.raises(ai.GenerationError, match="after 3 attempts"):
        ai.generate_responses(config, 1)
    assert len(calls) == 3


def test_reference_contains_joint_rows_frequencies_and_user_instructions(config, monkeypatch):
    references = [
        {int(key): value for key, value in profile("A", "First style").items()},
        {int(key): value for key, value in profile("B", "Second style").items()},
    ]
    calls = mock_chat(monkeypatch, [reply(profile())])
    ai.generate_responses(config, 1, reference=references, instructions="Responses from commuters")
    task = calls[0]["task"]
    assert task["instructions"] == "Responses from commuters"
    assert task["reference"]["choice_counts"]["1"] == {"1": 1, "2": 1}
    assert task["reference"]["example_profiles"][1]["3"] == "Second style"
    assert task["reference"]["example_profiles"][1]["1"] == 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"count": 0},
        {"count": True},
        {"count": 1, "batch_size": 0},
        {"count": 1, "max_retries": 4},
        {"count": 1, "timeout": float("nan")},
        {"count": 1, "timeout": float("inf")},
        {"count": 1, "timeout": True},
    ],
)
def test_invalid_parameters_make_no_request(config, monkeypatch, arguments):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must be rejected before opening ChatGPT")

    monkeypatch.setattr(ai, "_chat", unexpected)
    with pytest.raises(ai.GenerationError):
        ai.generate_responses(config, **arguments)


def test_invalid_reference_makes_no_request(config, monkeypatch):
    monkeypatch.setattr(ai, "_chat", lambda *args: pytest.fail("Invalid reference reached model"))
    with pytest.raises(ValueError):
        ai.generate_responses(config, 1, reference=[{1: "wrong"}])


def test_unsupported_form_makes_no_request(config, monkeypatch):
    config.unsupported = ["File upload"]
    monkeypatch.setattr(ai, "_chat", lambda *args: pytest.fail("Unsupported form reached model"))
    with pytest.raises(ai.GenerationError, match="File upload"):
        ai.generate_responses(config, 1)


def test_batches_and_corrections_reuse_one_private_browser(monkeypatch, config):
    opened, closed = [], []

    class Driver:
        def quit(self):
            closed.append(True)

    def open_chat(browser):
        opened.append(browser)
        return Driver()

    monkeypatch.setattr(ai, "_open_chat", open_chat)
    calls = mock_chat(monkeypatch, [reply(profile(), profile("bad")), reply(profile("B")), reply(profile())])
    assert len(ai.generate_responses(config, 3, batch_size=2)) == 3
    assert len(calls) == 3 and opened == ["chrome"] and closed == [True]


@pytest.mark.parametrize(
    "message", ["ChatGPT usage limit reached", "ChatGPT requires sign-in", "ChatGPT timed out"]
)
def test_actionable_transport_errors_stop_immediately(config, monkeypatch, message):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise ai.ChatGPTError(message)

    monkeypatch.setattr(ai, "_chat", fail)
    with pytest.raises(ai.GenerationError, match=message):
        ai.generate_responses(config, 1)
    assert len(calls) == 1


def test_keyboard_interrupt_propagates(config, monkeypatch):
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ai, "_chat", cancel)
    with pytest.raises(KeyboardInterrupt):
        ai.generate_responses(config, 1)


def test_completion_preserves_choices_order_and_retains_valid_rows(config, monkeypatch):
    partials = [{1: "A", 2: ["Run"]}, {1: "B", 2: ["Read, write"]}]
    wrong_first = {**profile("B"), "2": ["Run"]}
    second = profile("B", "Reason consistent with second profile")
    corrected_first = {**profile("A", "Reason consistent with first profile"), "2": ["Run"]}
    calls = mock_chat(monkeypatch, [reply(wrong_first, second), reply(corrected_first)])
    rows = ai.complete_responses(config, partials)
    assert [row[1] for row in rows] == ["A", "B"]
    assert [row[2] for row in rows] == [["Run"], ["Read, write"]]
    assert calls[1]["task"]["partial_profiles"] == [{"1": 1, "2": [2]}]
    assert calls[1]["task"]["response_count"] == 1
    assert "preserve the preselected" in calls[1]["task"]["corrections"][0]
    assert partials == [{1: "A", 2: ["Run"]}, {1: "B", 2: ["Read, write"]}]


def test_compact_option_ids_decode_to_exact_public_labels(config, monkeypatch):
    calls = mock_chat(monkeypatch, [reply({"1": 2, "2": [1, 2], "3": "A written answer", "4": ""})])
    rows = ai.generate_responses(config, 1)
    assert rows == [{1: "B", 2: ["Read, write", "Run"], 3: "A written answer", 4: ""}]
    properties = calls[0]["schema"]["properties"]["responses"]["items"]["properties"]
    assert properties["1"]["enum"] == [1, 2]
    assert properties["2"]["items"]["enum"] == [1, 2]
    assert calls[0]["task"]["questions"][1]["options"] == {"1": "Read, write", "2": "Run"}


@pytest.mark.parametrize("value", [True, 0, -1, 3])
def test_bad_compact_option_ids_are_corrected(config, monkeypatch, value):
    calls = mock_chat(monkeypatch, [reply({"1": value, "2": [1], "3": "Reason", "4": ""}), reply(profile())])
    assert ai.generate_responses(config, 1)[0][1] == "A"
    assert len(calls) == 2
    assert calls[1]["task"]["response_count"] == 1
    assert "option ID" in calls[1]["task"]["corrections"][0]


@pytest.mark.parametrize("partial", [{99: "Unknown"}, {1: "Invalid option"}, {2: ["Run", "Run"]}])
def test_invalid_preselected_answers_make_no_request(config, monkeypatch, partial):
    monkeypatch.setattr(ai, "_chat", lambda *args: pytest.fail("Invalid preselected answer reached model"))
    with pytest.raises(ValueError):
        ai.complete_responses(config, [partial])


def test_model_cannot_change_fixed_config_answers(config, monkeypatch):
    config.questions[0].fixed_value = "B"
    calls = mock_chat(monkeypatch, [reply(profile("A")), reply(profile("B"))])
    rows = ai.generate_responses(config, 1)
    assert rows[0][1] == "B"
    assert calls[0]["schema"]["properties"]["responses"]["items"]["properties"]["1"]["const"] == 2


def test_plain_json_code_fence_is_accepted(config, monkeypatch):
    mock_chat(monkeypatch, ["```json\n" + reply(profile()) + "\n```"])
    assert ai.generate_responses(config, 1)[0][1] == "A"


def test_unsupported_browser_stops_before_launch(config, monkeypatch):
    monkeypatch.setattr(ai, "_open_chat", lambda *args: pytest.fail("No browser should open"))
    with pytest.raises(ai.GenerationError, match="visible"):
        ai.generate_responses(config, 1, browser="headless")


def test_late_failure_checkpoints_valid_rows_and_resumes_only_pending(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    calls = []

    def first_attempt(driver, messages, schema, timeout, **kwargs):
        calls.append(json.loads(messages[-1]["content"]))
        if len(calls) == 1:
            return reply(profile("A", "Already generated"))
        raise ai.ChatGPTError("Usage limit reached")

    monkeypatch.setattr(ai, "_chat", first_attempt)
    with pytest.raises(ai.GenerationError, match="1/2 responses saved") as failure:
        ai.generate_responses(config, 2, batch_size=1, checkpoint=path)
    assert failure.value.completed == 1 and failure.value.requested == 2
    assert failure.value.checkpoint == path
    assert failure.value.partial_responses[0][3] == "Already generated"
    saved = json.loads(path.read_text())
    assert saved["responses"][0]["3"] == "Already generated" and saved["responses"][1] is None
    progress = []
    calls = mock_chat(monkeypatch, [reply(profile("B", "Newly generated"))])
    rows = ai.generate_responses(
        config, 2, checkpoint=path, progress=lambda done, total: progress.append((done, total))
    )
    assert [row[3] for row in rows] == ["Already generated", "Newly generated"]
    assert calls[0]["task"]["response_count"] == 1
    assert calls[0]["task"]["profile_numbers"] == [2]
    assert progress == [(1, 2), (2, 2)]


def test_complete_checkpoint_returns_without_opening_browser(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    mock_chat(monkeypatch, [reply(profile())])
    expected = ai.generate_responses(config, 1, checkpoint=path)
    monkeypatch.setattr(
        ai, "_open_chat", lambda *a: pytest.fail("Completed checkpoint must not open a browser")
    )
    assert ai.generate_responses(config, 1, checkpoint=path) == expected


@pytest.mark.parametrize("change", ["instructions", "reference", "fixed", "notes", "count"])
def test_changed_job_never_reuses_an_existing_checkpoint(config, monkeypatch, tmp_path, change):
    path = tmp_path / "generation.json"
    mock_chat(monkeypatch, [reply(profile())])
    ai.generate_responses(config, 1, checkpoint=path)
    options = {"count": 1}
    if change == "instructions":
        options["instructions"] = "Different population"
    elif change == "reference":
        options["reference"] = [{int(k): v for k, v in profile("B").items()}]
    elif change == "fixed":
        config.questions[0].fixed_value = "B"
    elif change == "notes":
        config.instructions = [
            {"page": 1, "before_question": 1, "title": "Adults only", "description": "Age 18 and over"}
        ]
    else:
        options["count"] = 2
    before = path.read_bytes()
    monkeypatch.setattr(
        ai, "_open_chat", lambda *a: pytest.fail("Mismatched checkpoint must not open a browser")
    )
    with pytest.raises(ai.GenerationError, match="different form, request, or reference"):
        ai.generate_responses(config, checkpoint=path, **options)
    assert path.read_bytes() == before


def test_cancelled_generation_retains_checkpoint_and_releases_lock(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    calls = []

    def cancel_after_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return reply(profile())
        raise KeyboardInterrupt()

    monkeypatch.setattr(ai, "_chat", cancel_after_first)
    with pytest.raises(KeyboardInterrupt):
        ai.generate_responses(config, 2, batch_size=1, checkpoint=path)
    assert json.loads(path.read_text())["responses"][1] is None
    mock_chat(monkeypatch, [reply(profile("B"))])
    assert len(ai.generate_responses(config, 2, checkpoint=path)) == 2


def test_partial_checkpoint_preserves_holes_and_preselected_profiles(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    partials = [{1: "A"}, {1: "B"}]
    mock_chat(monkeypatch, [reply(profile("wrong"), profile("B"))])
    with pytest.raises(ai.GenerationError):
        ai.complete_responses(config, partials, max_retries=0, checkpoint=path)
    calls = mock_chat(monkeypatch, [reply(profile("A"))])
    result = ai.complete_responses(config, partials, checkpoint=path)
    assert [row[1] for row in result] == ["A", "B"]
    assert calls[0]["task"]["profile_numbers"] == [1]
    assert calls[0]["task"]["partial_profiles"] == [{"1": 1}]


def test_checkpoint_saved_before_progress_callback_can_cancel(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    mock_chat(monkeypatch, [reply(profile())])

    def interrupt(done, total):
        assert json.loads(path.read_text())["responses"][0] is not None
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        ai.generate_responses(config, 1, checkpoint=path, progress=interrupt)
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("All data was checkpointed"))
    assert len(ai.generate_responses(config, 1, checkpoint=path)) == 1


def test_generation_checkpoint_contains_request_and_form_context(config, monkeypatch, tmp_path):
    config.sections = ["Eligibility"]
    config.instructions = [{"page": 1, "before_question": 1, "title": "Adults", "description": "18+ only"}]
    calls = mock_chat(monkeypatch, [reply(profile())])
    path = tmp_path / "generation.json"
    ai.generate_responses(config, 1, instructions="Commuters", checkpoint=path)
    task = calls[0]["task"]
    assert task["sections"] == ["Eligibility"]
    assert task["form_instructions"] == config.instructions
    assert task["questions"][0]["page"] == 1
    request = json.loads(path.read_text())["request"]
    assert request["instructions"] == "Commuters" and request["count"] == 1
    assert request["config"]["form_title"] == config.form_title


@pytest.mark.parametrize("corrupt", ["bad JSON", {"kind": "other"}])
def test_unrelated_checkpoint_file_is_not_overwritten(config, monkeypatch, tmp_path, corrupt):
    path = tmp_path / "generation.json"
    path.write_text(corrupt if isinstance(corrupt, str) else json.dumps(corrupt))
    before = path.read_bytes()
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Invalid checkpoint must not launch"))
    with pytest.raises(ai.GenerationError, match="Cannot resume"):
        ai.generate_responses(config, 1, checkpoint=path)
    assert path.read_bytes() == before


def test_checkpoint_lock_rejects_simultaneous_writer(config, monkeypatch, tmp_path):
    path = tmp_path / "generation.json"
    monkeypatch.setattr(ai, "_open_chat", lambda *a: pytest.fail("Locked checkpoint must not launch"))
    with ai._checkpoint_lock(path):
        with pytest.raises(ai.GenerationError, match="Cannot lock"):
            ai.generate_responses(config, 1, checkpoint=path)


def test_browser_cleanup_failure_does_not_discard_generation(config, monkeypatch, tmp_path):
    class Driver:
        def quit(self):
            raise ai.WebDriverException("Browser already closed")

    monkeypatch.setattr(ai, "_open_chat", lambda *a: Driver())
    mock_chat(monkeypatch, [reply(profile())])
    path = tmp_path / "generation.json"
    assert len(ai.generate_responses(config, 1, checkpoint=path)) == 1
    assert json.loads(path.read_text())["responses"][0] is not None
