from copy import deepcopy

import pytest

from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation_quality import Quality


def test_contact_rules_derive_from_name_and_preserve_supplied_answers():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Full name", "text", True),
            QuestionConfig(2, "Email", "text", True, validation=[[2, 102]]),
            QuestionConfig(3, "Telephone", "text", True),
            QuestionConfig(4, "Order notes", "textarea"),
        ],
    )
    policy = Quality(
        config,
        {
            "fields": {
                "Q2": {"email_domain": "demo.invalid", "name_from": "Q1"},
                "Q3": {"approved": ["(202) 555-0111", "(202) 555-0112"], "min_digits": 10},
            }
        },
    )
    candidate = {1: "Élodie River", 2: "incorrect@demo.invalid", 3: "000", 4: "Pick up at noon."}
    revised = policy.apply(candidate, 2)
    assert revised == {
        1: "Élodie River",
        2: "elodie.river@demo.invalid",
        3: "(202) 555-0112",
        4: "Pick up at noon.",
    }
    assert policy.issues(revised) == {}
    assert set(policy.issues(candidate)) == {2, 3}
    assert set(policy.issues(candidate, keys=["Q3"])) == {3}
    assert policy.presets({1: "Élodie River"}, 2, config.questions) == {
        2: "elodie.river@demo.invalid",
        3: "(202) 555-0112",
    }
    assert policy.presets({1: "Élodie River", "3": "(202) 555-0111"}, 2, [3]) == {}
    assert policy.presets({1: "Élodie River"}, 2, ["Q2"]) == {2: "elodie.river@demo.invalid"}
    assert policy.apply(candidate, 2, protected={2, 3}) == candidate
    assert candidate[3] == "000"
    prompt = str(policy.brief(config.questions))
    assert "at least 10 digits" in prompt
    assert "202) 555" not in prompt


def test_routing_fact_applies_only_to_the_active_route_and_fails_closed():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        sections=["Start", "Follow up", "Readback"],
        questions=[
            QuestionConfig(
                1, "Route", "radio", True, options=["Short", "Full"], branches={"Short": 3, "Full": 2}
            ),
            QuestionConfig(2, "Skipped branch note", "textarea", page=2),
            QuestionConfig(3, "Readback", "radio", True, options=["Visited", "Skipped"], page=3),
        ],
    )
    policy = Quality(config, {"fields": {"3": {"route_values": {"1,3": "Skipped"}}}})
    row = {1: "Short", 2: "", 3: "Visited"}
    assert set(policy.issues(row)) == {3}
    assert policy.apply(row, 1) == {1: "Short", 2: "", 3: "Skipped"}
    assert policy.presets({1: "Short"}, 1, config.questions) == {}
    assert policy.presets({1: "Short"}, 1, config.questions, route=(1, 3)) == {3: "Skipped"}
    assert policy.issues({**row, 1: "Full"}) == {3: "No configured value matches this response's route."}
    with pytest.raises(ValueError, match="controls routing"):
        Quality(config, {"fields": {"1": {"exact": "Full"}}})


def test_media_evidence_policy_and_profile_dependencies_are_scoped():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        form_title="Equipment readback",
        questions=[
            QuestionConfig(1, "Asset type", "text", True),
            QuestionConfig(2, "Observed status", "text", True),
            QuestionConfig(3, "Which image matches the installed unit?", "radio", True, ["A", "B"]),
            QuestionConfig(4, "Explain the diagnosis", "textarea", True),
        ],
    )
    spec = {"facts": ["Q2"], "dependencies": {"4": ["Q1", 2]}, "style": "Write a short service note."}
    row = {1: "Gateway", 2: "Operational", 3: "A", 4: "Running normally."}
    policy = Quality(config, spec)
    assert policy.anchors(row, [config.questions[-1]]) == {"1": "Gateway", "2": "Operational"}
    assert policy.review_keys(row) == (3, 4)
    assert set(policy.evidence_issues(row)) == {3}
    with_evidence = Quality(config, {**spec, "evidence": {"Q3": "The supplied image shows a gateway."}})
    assert with_evidence.evidence_issues(row) == {}
    brief = with_evidence.brief(config.questions[-2:])
    assert brief["purpose"] == "Equipment readback"
    assert brief["supplied_evidence"] == {"3": "The supplied image shows a gateway."}
    assert brief["answer_dependencies"] == {"4": ["1", "2"]}
    assert "Write a short service note." in brief["writing_guidance"]
    assert "supplied_evidence" not in with_evidence.brief(config.questions[:2])
    assert not Quality(config, {**spec, "allow_placeholders": ["Q3"]}).evidence_issues(row)


def test_batch_quality_reports_patterns_in_response_order_without_sharing_answers():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Choice", "radio", options=["A", "B", "C"]),
            QuestionConfig(2, "Phone", "text"),
            QuestionConfig(3, "Notes", "textarea"),
        ],
    )
    rows = {
        i: {
            1: ["A", "B", "C"][i % 3],
            2: "(202) 555-0100" if i % 2 else "2025550100",
            3: f"Please bring the order to the side entrance at station {i}.",
        }
        for i in reversed(range(30))
    }
    original = deepcopy(rows)
    policy = Quality(config)
    signals = policy.batch_signals(rows)
    assert signals["choice_cycles"][0]["row_interval"] == 3
    assert signals["repeated_contacts"] == [{"question": 2, "repeated_answers": 29}]
    assert signals["repeated_text"] == [{"question": 3, "repeated_templates": 29}]
    assert "side entrance" not in str(signals)
    assert "555" not in str(signals)
    assert rows == original
    assert "choice_cycles" not in policy.batch_signals([{1: "A"}] * 30)


def test_only_high_risk_simple_answers_need_semantic_review():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Reason", "text"),
            QuestionConfig(2, "Order comments", "textarea"),
            QuestionConfig(3, "Was the embedded video accessible?", "radio", True, ["Yes", "Not checked"]),
        ],
    )
    policy = Quality(config)
    row = {1: "Family party", 2: "Please wrap separately.", 3: "Yes"}
    assert policy.review_keys(row) == (3,)
    assert policy.evidence_issues(row)
    assert policy.evidence_issues({**row, 3: "Not checked"}) == {}
    assert policy.evidence_issues(row, protected={3}) == {}


def test_explicit_evidence_and_placeholder_fields_are_reviewed_without_title_heuristics():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "What did you see?", "textarea"),
            QuestionConfig(2, "Device color", "text"),
            QuestionConfig(3, "Extra details", "textarea"),
        ],
    )
    policy = Quality(config, {"allow_placeholders": [1], "evidence": {"2": "The device is blue."}})
    assert policy.review_keys({1: "Not viewed", 2: "Blue", 3: "Additional context"}) == (1, 2)
    assert policy.review_keys({1: "", 2: "Blue"}) == (2,)


def test_planned_coverage_is_seeded_weighted_and_preserves_presets():
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(1, "Choice", "radio", options=["A", "B", "C"], weights=[3, 1, 0]),
            QuestionConfig(2, "Reason", "text"),
            QuestionConfig(3, "Color", "dropdown", options=["Red", "Blue"]),
        ],
    )
    policy = Quality(config, {"coverage": ["Q1", 3]})
    partials = [{"1": "B", 2: "Existing text"}] + [{} for _ in range(19)]
    result = policy.coverage_partials(20, partials, "stable-job")
    assert result == policy.coverage_partials(20, partials, "stable-job")
    assert result[0]["1"] == "B" and result[0][2] == "Existing text"
    assert partials[1] == {}
    assert sum(row.get(1, row.get("1")) == "A" for row in result) == 15
    assert sum(row[3] == "Blue" for row in result) == 10
    with pytest.raises(ValueError, match="guide"):
        Quality(config, {"coverage": [1]}, reference=[{1: "B", 3: "Red"}])
    with pytest.raises(ValueError, match="cannot be preselected"):
        Quality(config, {"coverage": [2]})


@pytest.mark.parametrize(
    "spec, error",
    [
        ({"fields": {"Q3": {"exact": "A"}}}, "unknown question"),
        ({"fields": {"Q1": {"approved": [" "], "min_digits": 10}}}, "nonblank"),
        ({"fields": {"Q1": {"exact": "A", "approved": ["B"]}}}, "at most one source"),
        ({"fields": {"Q1": {"min_digits": True}}}, "min_digits"),
        ({"fields": {"Q1": {"email_domain": "bad/domain", "name_from": "Q2"}}}, "valid test email domain"),
        ({"dependencies": {"Q1": ["Q1"]}}, "different|other question"),
        ({"facts": ["1", "Q1"]}, "duplicate"),
    ],
)
def test_invalid_policy_rejected_before_model_request(spec, error):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[QuestionConfig(1, "Phone", "text"), QuestionConfig(2, "Name", "text")],
    )
    with pytest.raises(ValueError, match=error):
        Quality(config, spec)
