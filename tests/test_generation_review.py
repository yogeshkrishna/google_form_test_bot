"""Verify review identity and evidence grounding."""

import json

import pytest

from gformbot.generation_review import build_review_request, parse_review_response


@pytest.fixture
def profiles():
    return {
        7: {
            "answers": {5: "Gateway", 6: "Replaced the failed camera."},
            "review_keys": [6],
            "facts": {"asset": "Gateway"},
        },
        18: {"answers": {5: "Gateway", 6: "Replaced the failed gateway."}, "review_keys": [6]},
        19: {"answers": {8: {"choices": ["Courier"], "other": "Local pickup"}}},
    }


def _finding(question=6, quote="failed camera"):
    return {
        "question": question,
        "kind": "contradiction",
        "reason": "The asset is a gateway but the note describes a camera.",
        "evidence": [{"question": question, "quote": quote}],
    }


def _record(response, *issues):
    return json.dumps({"response": response, "issues": issues})


def test_review_prompt_preserves_form_text_facts_and_exact_review_scope(profiles):
    form = {"description": "Report repairs.", "questions": {"6": "What did you do?"}}
    messages = build_review_request(form, profiles)
    assert [message["role"] for message in messages] == ["system", "user"]
    payload = json.loads(messages[1]["content"])
    assert payload["form"] == form
    assert payload["profiles"]["7"]["facts"] == {"asset": "Gateway"}
    assert payload["profiles"]["7"]["review_keys"] == [6]


def test_review_parser_preserves_valid_records_after_bad_line(profiles):
    report = "\n".join(
        [
            _record(18),
            '{"response":7,"issues":[',
            _record(7, _finding()),
            _record(19, _finding(question=8, quote="Local pickup")),
            '{"response":20,',
        ]
    )
    assert parse_review_response(report, profiles) == {
        7: [_finding()],
        18: [],
        19: [_finding(question=8, quote="Local pickup")],
    }


@pytest.mark.parametrize(
    "report",
    [
        _record(7, _finding(question=5, quote="Gateway")),
        _record(7, _finding(quote="misquoted camera")),
        _record(7, {**_finding(), "reason": "\x1b[31mclaim"}),
        _record(7, {**_finding(), "evidence": [{"question": 5, "quote": "Gateway"}]}),
        _record(7) + "\n" + _record(7, _finding()),
        '{"response":7,"response":18,"issues":[]}',
        _record(True),
        _record(77),
    ],
)
def test_review_parser_rejects_untrusted_or_ambiguous_findings(profiles, report):
    assert parse_review_response(report, profiles) == {}


def test_correction_names_only_pending_ids_and_explains_evidence_format(profiles):
    correction = build_review_request({}, {18: profiles[18]}, retry=True)
    assert "Required response IDs: 18." in correction[0]["content"]
    assert "could not be validated" in correction[0]["content"]
    assert "copy quoted text verbatim from answers" in correction[0]["content"]
    assert set(json.loads(correction[1]["content"])["profiles"]) == {"18"}
