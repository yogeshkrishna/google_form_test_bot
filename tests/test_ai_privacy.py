"""Outbound URL redaction leaves local form and supplied-response identity intact."""

import json
from copy import deepcopy

import pytest

from gformbot import ai
from gformbot.config import QuestionConfig, RunConfig, config_to_dict
from gformbot.inspector import schema_fingerprint

URL = "https://docs.google.com/forms/d/e/privacy_test/viewform"
SECRET = "SYNTHETIC_PRIVACY_SENTINEL"
PRIVATE = f"https://example.test/join?token={SECRET}&group=public"
PUBLIC = "https://chat.whatsapp.com/PublicInvite"


def capture_chat(monkeypatch, replies):
    class Driver:
        def quit(self):
            pass

    calls = []
    replies = iter(replies)
    monkeypatch.setattr(ai, "_open_chat", lambda *_: Driver())

    def chat(driver, messages, schema, *args, **kwargs):
        calls.append(
            {
                "messages": deepcopy(messages),
                "schema": deepcopy(schema),
                "task": json.loads(messages[-1]["content"]),
            }
        )
        return json.dumps({"responses": [next(replies)]})

    monkeypatch.setattr(ai, "_chat", chat)
    return calls


def test_outbound_context_redacts_urls_without_changing_metadata_or_checkpoint(tmp_path, monkeypatch):
    config = RunConfig(
        URL,
        form_title=f"Visit {PRIVATE}",
        form_description=f'Keep quotes: "{PRIVATE}"; public: {PUBLIC}',
        sections=[f"Section {PRIVATE}"],
        instructions=[{"page": 1, "before_question": 1, "title": PRIVATE, "description": PUBLIC}],
        questions=[
            QuestionConfig(1, f"Choice {PRIVATE}", "radio", True, options=[PRIVATE, PUBLIC]),
            QuestionConfig(2, "Reason", "text", True, description=PRIVATE),
        ],
    )
    reference = [{1: PRIVATE, 2: f"Reference {PRIVATE}"}]
    before, before_reference = config_to_dict(config), deepcopy(reference)
    fingerprint = schema_fingerprint(config)
    checkpoint = tmp_path / "generation.json"
    calls = capture_chat(monkeypatch, [{"1": 1, "2": "Fictional reason"}])
    rows = ai.generate_responses(
        config, 1, reference=reference, instructions=f"Use {PRIVATE}", checkpoint=checkpoint
    )

    sent = json.dumps(calls[0]["messages"])
    assert SECRET not in sent
    assert SECRET not in json.dumps(calls[0]["schema"])
    assert "REDACTED" in sent and PUBLIC in sent
    assert calls[0]["task"]["form_description"].startswith('Keep quotes: "https://')
    assert rows == [{1: PRIVATE, 2: "Fictional reason"}]
    assert config_to_dict(config) == before and reference == before_reference
    assert schema_fingerprint(config) == fingerprint
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["request"]["config"] == before
    assert payload["request"]["reference"] == [{"1": PRIVATE, "2": f"Reference {PRIVATE}"}]
    assert SECRET in checkpoint.read_text(encoding="utf-8")
    assert (
        ai.generate_responses(
            config, 1, reference=reference, instructions=f"Use {PRIVATE}", checkpoint=checkpoint
        )
        == rows
    )
    assert len(calls) == 1
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["job_key"] == payload["job_key"]


@pytest.mark.parametrize(
    "restricted",
    [
        f"https://username:{SECRET}@example.test/path",
        f"https://accounts.google.com/AccountChooser?continue={SECRET}",
        f"https://docs.google.com/forms/d/e/example/viewform?edit2={SECRET}",
    ],
)
def test_outbound_omits_credentials_account_and_edit_response_links(monkeypatch, restricted):
    config = RunConfig(
        URL, form_description=f"{restricted} {PUBLIC}", questions=[QuestionConfig(1, "Name", "text")]
    )
    calls = capture_chat(monkeypatch, [{"1": "Fictional"}])
    ai.generate_responses(config, 1)
    sent = json.dumps(calls[0]["messages"])
    assert SECRET not in sent and restricted not in sent
    assert "[link omitted]" in sent and PUBLIC in sent
    assert restricted in config.form_description


def test_retry_context_and_corrections_do_not_repeat_url_secrets(monkeypatch):
    config = RunConfig(
        URL,
        form_description=PRIVATE,
        questions=[QuestionConfig(1, "Name", "text", True, validation=[[6, 203, ["2"], PRIVATE]])],
    )
    calls = capture_chat(monkeypatch, [{"1": "x"}, {"1": "Fictional"}])
    assert ai.generate_responses(config, 1) == [{1: "Fictional"}]
    assert len(calls) == 2 and calls[1]["task"]["corrections"]
    assert SECRET not in json.dumps([call["messages"] for call in calls])
    assert config.questions[0].validation[0][3] == PRIVATE


@pytest.mark.parametrize("preset", ["fixed", "partial"])
def test_redacted_preset_echo_restores_exact_local_value(monkeypatch, preset):
    config = RunConfig(
        URL,
        questions=[
            QuestionConfig(
                1, "Supplied link", "text", True, fixed_value=PRIVATE if preset == "fixed" else None
            ),
            QuestionConfig(2, "Generated explanation", "text", True),
        ],
    )
    partials = [{1: PRIVATE}]
    calls = capture_chat(monkeypatch, [{"1": PRIVATE.replace(SECRET, "REDACTED"), "2": "Fictional"}])
    if preset == "fixed":
        rows = ai.generate_responses(config, 1, max_retries=0)
    else:
        rows = ai.complete_responses(config, partials, max_retries=0)
    assert SECRET not in json.dumps(calls[0]["messages"])
    assert SECRET not in json.dumps(calls[0]["schema"])
    assert rows == [{1: PRIVATE, 2: "Fictional"}]
    assert partials == [{1: PRIVATE}]
    assert config.questions[0].fixed_value == (PRIVATE if preset == "fixed" else None)


@pytest.mark.parametrize("kind,choices", [("radio", []), ("checkbox", [PRIVATE])])
@pytest.mark.parametrize("preset_source", ["fixed", "partial"])
def test_redacted_other_echo_preserves_original_choices_and_custom_text(
    monkeypatch, kind, choices, preset_source
):
    preset = {"choices": choices, "other": PRIVATE}
    config = RunConfig(
        URL,
        questions=[
            QuestionConfig(
                1,
                "Supplied choice",
                kind,
                True,
                options=[PRIVATE, PUBLIC],
                allow_other=True,
                fixed_value=deepcopy(preset) if preset_source == "fixed" else None,
            ),
            QuestionConfig(2, "Explanation", "text", True),
        ],
    )
    reply = {
        "1": {"choices": [1] if choices else [], "other": PRIVATE.replace(SECRET, "REDACTED")},
        "2": "Fictional",
    }
    calls = capture_chat(monkeypatch, [reply])
    partials = [{1: deepcopy(preset)}]
    if preset_source == "fixed":
        rows = ai.generate_responses(config, 1, max_retries=0)
    else:
        rows = ai.complete_responses(config, partials, max_retries=0)
    assert rows == [{1: preset, 2: "Fictional"}]
    assert partials == [{1: preset}]
    assert SECRET not in json.dumps(calls[0]["messages"])
    assert SECRET not in json.dumps(calls[0]["schema"])


@pytest.mark.parametrize("replacement", ["Changed", PRIVATE.replace(SECRET, "REDACTED") + "-changed"])
def test_redaction_does_not_accept_arbitrary_changed_presets(monkeypatch, replacement):
    config = RunConfig(URL, questions=[QuestionConfig(1, "Link", "text", True)])
    capture_chat(monkeypatch, [{"1": replacement}])
    with pytest.raises(ai.GenerationError, match="preserve the preselected"):
        ai.complete_responses(config, [{1: PRIVATE}], max_retries=0)
