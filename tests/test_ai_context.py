import copy
import json

from gformbot.ai_context import compact_https, history_keys, pack_history, selected_history
from gformbot.config import QuestionConfig, RunConfig


def test_bounded_history_keeps_exact_dependencies_persona_routing_presets_and_recent_values():
    questions = [QuestionConfig(i, f"Prompt {i}", "text") for i in range(1, 61)]
    questions[7].text = "S03-Q09 — Old department"
    questions[8].entry_id = "source_9"
    questions[9].text = "Detailed original preference"
    questions[10].fixed_value = "Fixed"
    questions[11].branches = {"Choose": 2}
    questions[12].text = "Email"
    current = QuestionConfig(
        61,
        "Explain a recent choice",
        "textarea",
        description="Use Q7, S03-Q09, entry.source_9 and Detailed original preference.",
    )
    config = RunConfig(url="unused", questions=[*questions, current])
    row = {q.index: f"Original {q.index}" for q in questions}
    row[7] = {"choices": ["café, 😀"], "other": "مرحبا"}
    original = copy.deepcopy(row)

    keys = history_keys(config, [current], "Create coherent fictional profiles.")
    kept = selected_history(row, keys, presets={14: "Original 14"})

    assert {str(i) for i in range(1, 15)} <= kept.keys()
    assert {str(i) for i in range(45, 61)} <= kept.keys()
    assert "20" not in kept and "30" not in kept
    assert kept["7"] == row[7]
    kept["7"]["choices"].append("Changed context copy")
    assert row == original


def test_vague_question_reference_preserves_available_history():
    questions = [QuestionConfig(i, f"Prompt {i}", "text") for i in range(1, 40)]
    current = QuestionConfig(40, "Explain the answers above", "textarea")
    config = RunConfig(url="unused", questions=[*questions, current])
    row = {q.index: f"Value {q.index}" for q in questions}
    assert selected_history(row, history_keys(config, [current])) == {str(k): v for k, v in row.items()}


def test_source_question_alias_does_not_match_unrelated_global_question_number():
    questions = [QuestionConfig(i, f"Prompt {i}", "text") for i in range(1, 60)]
    questions[7].text = "S03-Q09 — Original sensor category"
    current = QuestionConfig(60, "Explain", "textarea", description="Refer to S03-Q09.")
    config = RunConfig(url="unused", questions=[*questions, current])
    keys = history_keys(config, [current])
    assert 8 in keys and 9 not in keys


def test_current_section_directions_can_reference_an_older_answer():
    questions = [QuestionConfig(i, f"Prompt {i}", "text") for i in range(1, 40)]
    current = QuestionConfig(40, "Explain", "textarea", page=2)
    config = RunConfig(
        url="unused",
        questions=[*questions, current],
        instructions=[{"page": 2, "description": "Carry forward answer key 19."}],
    )
    assert 19 in history_keys(config, [current])


def test_navigation_media_and_general_coherence_do_not_expand_all_answers():
    questions = [QuestionConfig(i, f"Prompt {i}", "text") for i in range(1, 60)]
    current = QuestionConfig(
        60,
        "Was the previous branch completed?",
        "text",
        description="Inspect the image above. Rate independently of the prior questions.",
    )
    config = RunConfig(url="unused", questions=[*questions, current])
    keys = history_keys(config, [current], "Keep responses consistent with previous answers.")
    assert 20 not in keys
    assert len(selected_history({i: str(i) for i in range(1, 60)}, keys)) == 22


def test_repeated_title_boilerplate_does_not_pin_every_field_but_unique_terms_do():
    questions = [QuestionConfig(i, f"Short text validation field {i}", "text") for i in range(1, 60)]
    questions[18].text = "Preferred kayaking location"
    current = QuestionConfig(
        60, "Short text validation field", "text", description="Explain your kayaking preference."
    )
    config = RunConfig(url="unused", questions=[*questions, current])
    keys = history_keys(config, [current])
    assert 19 in keys
    assert 20 not in keys


def test_packed_history_preserves_exact_prior_values_and_only_current_profiles():
    shared = {
        "previous_questions": {"1": "Duplicate title", "2": "Duplicate title", "3": "Empty", "4": "Unused"},
        "route": [1, 4],
    }
    all_profiles = {
        "1": {"response_number": 12, "earlier_answers": {"1": ["café, 😀", "مرحبا"], "2": "", "3": None}},
        "2": {"response_number": 15, "earlier_answers": {"1": ["café, 😀", "مرحبا"], "2": []}},
        "3": {"response_number": 20, "earlier_answers": {"1": "Must not be shared"}},
    }
    current = {key: all_profiles[key] for key in ("1", "2")}
    original = copy.deepcopy((shared, current))

    packed_shared, packed_profiles = pack_history(shared, current)
    columns = packed_shared["history"]["columns"]
    for identity, profile in packed_profiles.items():
        unpacked = {
            key: values[cell - 1]
            for (key, _, values), cell in zip(columns, profile["earlier_answers"], strict=True)
            if cell
        }
        assert unpacked == current[identity]["earlier_answers"]
        assert profile["response_number"] == current[identity]["response_number"]
    assert columns[0][2] == [["café, 😀", "مرحبا"]]
    assert [column[:2] for column in columns[:2]] == [["1", "Duplicate title"], ["2", "Duplicate title"]]
    assert packed_profiles["1"]["earlier_answers"][2] == 1
    assert packed_profiles["2"]["earlier_answers"][2] == 0
    assert "Must not be shared" not in json.dumps(packed_shared, ensure_ascii=False)
    assert "Unused" not in json.dumps(packed_shared, ensure_ascii=False)
    assert (shared, current) == original
    assert packed_shared["route"] == shared["route"]


def test_https_compaction_keeps_constraints_and_exact_encoded_fixed_values():
    questions = [
        QuestionConfig(1, "Code", "text", validation=[[4, 301, ["T-[0-9]+"]]]),
        QuestionConfig(2, "Choices", "checkbox", min_select=2, max_select=2, fixed_value=["A", "B"]),
        QuestionConfig(3, "Time", "time", validation=[[99, 99]]),
    ]
    fallback = {"type": "string", "pattern": "08:00|09:00"}
    routing = {"choices": {"2": {"1": 3}}}
    context = {
        "questions": [
            {
                "key": str(q.index),
                "question": q.text,
                "description": "Exact prompt",
                "validation": q.validation,
                **({"options": {"2": "B", "1": "A"}, "required": True} if q.index == 2 else {}),
            }
            for q in questions
        ],
        "answer_schema": {"properties": {"1": {"type": "string"}, "2": {"const": [1, 2]}, "3": fallback}},
        "form_instructions": [{"title": "Follow these instructions exactly"}],
        "routing": routing,
    }

    result, guidance = compact_https(context, RunConfig(url="unused", questions=questions))

    table = result["questions"]
    rules = table["columns"].index("validation")
    fixed = table["columns"].index("fixed")
    assert table["rows"][0][rules] == ["Matches pattern: T-[0-9]+"]
    assert table["rows"][1][rules] == {"minimum_selections": 2, "maximum_selections": 2}
    assert table["rows"][1][fixed] == [1, 2]
    assert table["rows"][1][table["columns"].index("options")] == ["A", "B"]
    assert table["rows"][1][table["columns"].index("required")] is True
    assert table["rows"][2][rules] == {"raw": [[99, 99]], "schema": fallback}
    assert table["shared"]["description"] == ["Exact prompt"]
    assert result["form_instructions"] == context["form_instructions"]
    assert result["routing"] == routing
    assert "UTF-16" in guidance and "ID 1" in guidance and "1-based reference" in guidance
