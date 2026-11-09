"""Adaptive generation keeps independently validated profiles in one bounded conversation."""

import json

import pytest

from gformbot import ai
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def config(monkeypatch):
    closed, opened = [], []

    class Driver:
        def quit(self):
            closed.append(True)

    monkeypatch.setattr(ai, "_open_chat", lambda browser: opened.append(browser) or Driver())
    monkeypatch.setattr(ai, "_new_chat", lambda driver: None)
    value = RunConfig(
        "https://docs.google.com/forms/d/e/adaptive/viewform",
        questions=[
            QuestionConfig(1, "Choose", "radio", True, options=["A", "B"]),
            QuestionConfig(2, "Reason", "text", True),
        ],
    )
    value._closed, value._opened = closed, opened
    return value


def record(identity, choice=1, reason="A useful fictional answer"):
    return json.dumps({"profile_id": identity, "answers": {"1": choice, "2": reason}})


def install_chat(monkeypatch, handler):
    calls = []

    def chat(driver, messages, schema, timeout, **kwargs):
        task = json.loads(messages[-1]["content"])
        calls.append(task)
        return handler(task, len(calls))

    monkeypatch.setattr(ai, "_chat", chat)
    return calls


def test_hundred_short_profiles_take_five_requests_and_context_is_not_repeated(config, monkeypatch):
    calls = install_chat(monkeypatch, lambda task, _: "\n".join(record(i) for i in task["profile_numbers"]))
    progress = []
    rows = ai.generate_responses(config, 100, progress=lambda *values: progress.append(values))
    assert len(rows) == 100 and len(calls) == 5
    assert calls[0]["questions"] and calls[0]["answer_schema"]
    assert all("questions" not in task and "answer_schema" not in task for task in calls[1:])
    assert progress == [(20, 100), (40, 100), (60, 100), (80, 100), (100, 100)]
    assert config._opened == ["chrome"] and config._closed == [True]


def test_truncated_record_retains_preceding_profiles_and_halves_batch(config, monkeypatch):
    def response(task, turn):
        if turn == 1:
            return record(1, reason="Keep me") + '\n{"profile_id":2,"answers":'
        return "\n".join(record(i) for i in reversed(task["profile_numbers"]))

    calls = install_chat(monkeypatch, response)
    rows = ai.generate_responses(config, 8, batch_size=8)
    assert rows[0][2] == "Keep me"
    assert calls[1]["profile_numbers"] == [2, 3, 4, 5]
    assert all(1 not in task["profile_numbers"] for task in calls[1:])
    assert all(set(row) == {1, 2} for row in rows)


def test_duplicate_and_unknown_ids_never_become_accepted_profiles(config):
    text = "\n".join([record(1), record(1, choice=2), record(2), record(999)])
    valid, errors, incomplete = ai._validate_records(config, text, [1, 2])
    assert set(valid) == {2}
    assert any("duplicate" in message for message in errors)
    assert any("requested integer" in message for message in errors)
    assert not incomplete


def test_complete_records_may_arrive_out_of_order_and_presets_stay_exact(config, monkeypatch):
    partials = [{1: "A"}, {1: "B"}]
    install_chat(monkeypatch, lambda *args: record(2, choice=2) + "\n" + record(1))
    assert [row[1] for row in ai.complete_responses(config, partials)] == ["A", "B"]
    assert partials == [{1: "A"}, {1: "B"}]


def test_validation_failure_retries_only_bad_id(config, monkeypatch):
    calls = install_chat(
        monkeypatch, lambda task, turn: record(2) + "\n" + record(1, choice=7) if turn == 1 else record(1)
    )
    assert len(ai.generate_responses(config, 2)) == 2
    assert calls[1]["profile_numbers"] == [1]
    assert "7" not in " ".join(calls[1]["corrections"])


def test_bad_content_has_bounded_corrections_and_no_random_fallback(config, monkeypatch):
    calls = install_chat(monkeypatch, lambda *args: "not JSON")
    with pytest.raises(ai.GenerationError, match="after 3 attempts"):
        ai.generate_responses(config, 1)
    assert len(calls) == 3 and config._closed == [True]


def test_timeout_salvages_independent_complete_records_without_resending(config, monkeypatch):
    def fail(*args):
        raise ai.ChatGPTError("Interrupted", partial_content=record(1) + '\n{"profile_id":2')

    calls = install_chat(monkeypatch, fail)
    with pytest.raises(ai.GenerationError, match="Interrupted") as failure:
        ai.generate_responses(config, 3)
    assert failure.value.completed == 1 and failure.value.requested == 3
    assert failure.value.partial_responses == [{1: "A", 2: "A useful fictional answer"}]
    assert len(calls) == 1 and config._closed == [True]


def test_estimator_distinguishes_choice_text_constraints_and_long_instructions(config):
    choices = RunConfig(config.url, questions=[QuestionConfig(1, "Pick", "radio", True, options=["A"])])
    text = RunConfig(config.url, questions=[QuestionConfig(1, "Explain", "textarea", True)])
    assert ai._estimate_profile_size(choices) < ai._estimate_profile_size(text)
    assert ai._estimate_profile_size(text, "Give 1000 words per answer") > ai._estimate_profile_size(text)
    assert ai._estimate_profile_size(text, reference=[{1: "x" * 2000}]) > ai._estimate_profile_size(text)


def test_oversized_context_fails_before_browser(config, monkeypatch):
    config.questions[0].description = "x" * 181_000
    monkeypatch.setattr(ai, "_open_chat", lambda *args: pytest.fail("Oversized context opened browser"))
    with pytest.raises(ai.GenerationError, match="context is too large"):
        ai.generate_responses(config, 1)


def test_growth_uses_observed_output_size(config, monkeypatch):
    config.questions[1].qtype = "textarea"
    calls = install_chat(
        monkeypatch, lambda task, _: "\n".join(record(i, reason="x") for i in task["profile_numbers"])
    )
    ai.generate_responses(config, 50, instructions="Write 200 words per answer")
    assert calls[0]["response_count"] < calls[1]["response_count"]
    assert max(task["response_count"] for task in calls) <= 20


def test_conversation_renews_before_context_growth_becomes_excessive(config, monkeypatch):
    config.form_description = "x" * 155_000
    resets = []
    monkeypatch.setattr(ai, "_new_chat", lambda driver: resets.append(True))
    calls = install_chat(
        monkeypatch, lambda task, _: "\n".join(record(i, reason="x" * 1100) for i in task["profile_numbers"])
    )
    ai.generate_responses(config, 60)
    assert resets
    assert sum("questions" in task for task in calls) == len(resets) + 1


def test_duplicate_answer_json_keys_are_not_silently_overwritten(config):
    valid, _, incomplete = ai._validate_records(
        config, '{"profile_id":1,"answers":{"1":1,"1":2,"2":"answer"}}', [1]
    )
    assert not valid and incomplete


def test_later_website_failure_retains_earlier_complete_profiles(config, monkeypatch):
    def response(task, turn):
        if turn == 2:
            raise ai.ChatGPTError("Network interrupted")
        return "\n".join(record(i, reason=f"Profile {i}") for i in task["profile_numbers"])

    calls = install_chat(monkeypatch, response)
    with pytest.raises(ai.GenerationError, match="Network interrupted") as failure:
        ai.generate_responses(config, 30)
    assert failure.value.completed == 20 and len(failure.value.partial_responses) == 20
    assert len(calls) == 2
    assert failure.value.partial_responses[-1][2] == "Profile 20"


def test_context_renewal_failure_preserves_accepted_profiles(config, monkeypatch):
    config.form_description = "x" * 170_000

    def renewal(driver):
        raise ai.ChatGPTError("New conversation unavailable")

    monkeypatch.setattr(ai, "_new_chat", renewal)
    calls = install_chat(
        monkeypatch, lambda task, _: "\n".join(record(i, reason="x" * 1100) for i in task["profile_numbers"])
    )
    with pytest.raises(ai.GenerationError, match="New conversation unavailable") as failure:
        ai.generate_responses(config, 30)
    assert failure.value.completed == 20 and len(failure.value.partial_responses) == 20
    assert len(calls) == 1 and config._closed == [True]


def test_known_preset_output_size_shrinks_before_sending(config, monkeypatch):
    partials = [{2: "x" * 9000} for _ in range(10)]
    calls = install_chat(
        monkeypatch,
        lambda task, _: "\n".join(record(i, reason=partials[i - 1][2]) for i in task["profile_numbers"]),
    )
    rows = ai.complete_responses(config, partials)
    assert len(rows) == 10
    assert calls[0]["response_count"] <= 2
    assert all(task["response_count"] <= 2 for task in calls)
    assert config._opened == ["chrome"]


def test_large_partial_prompt_is_checked_in_addition_to_shared_context(config, monkeypatch):
    config.form_description = "x" * 173_000
    monkeypatch.setattr(
        ai, "_open_chat", lambda *args: pytest.fail("Oversized complete prompt opened browser")
    )
    with pytest.raises(ai.GenerationError, match="single response's prompt"):
        ai.complete_responses(config, [{2: "x" * 5000}])


def test_known_oversized_single_answer_fails_before_browser(config, monkeypatch):
    monkeypatch.setattr(ai, "_open_chat", lambda *args: pytest.fail("Oversized reply opened browser"))
    with pytest.raises(ai.GenerationError, match="single response's prompt"):
        ai.generate_responses(config, 1, instructions="Write 5000 words per answer")


def test_outgoing_followup_bytes_trigger_context_renewal(config, monkeypatch):
    config.form_description = "x" * 144_000
    partials = [{2: "x" * 9000} for _ in range(4)]
    resets = []
    monkeypatch.setattr(ai, "_new_chat", lambda driver: resets.append(True))
    calls = install_chat(
        monkeypatch,
        lambda task, _: "\n".join(record(i, reason=partials[i - 1][2]) for i in task["profile_numbers"]),
    )
    assert len(ai.complete_responses(config, partials)) == 4
    assert resets
    assert sum("questions" in task for task in calls) == len(resets) + 1
