"""Small helpers for explicit custom-choice answers; ordinary values stay unchanged."""

import json
from copy import deepcopy


def copy_value(value):
    return deepcopy(value) if isinstance(value, dict) else value.copy() if isinstance(value, list) else value


def other_answer(question, value):
    if not question.allow_other:
        raise ValueError(f"Q{question.index}: this question has no Other option.")
    if not isinstance(value, dict) or set(value) != {"choices", "other"}:
        raise ValueError(f"Q{question.index}: Other needs an object with choices and other text.")
    choices, text = value["choices"], value["other"]
    if (
        not isinstance(choices, list)
        or any(not isinstance(v, str) or v not in question.options for v in choices)
        or len(set(choices)) != len(choices)
    ):
        raise ValueError(f"Q{question.index}: Other choices must be distinct exact option labels.")
    if not isinstance(text, str) or not text.strip() or any(c in text for c in "\r\n"):
        raise ValueError(f"Q{question.index}: Other needs nonempty single-line text.")
    count = len(choices) + 1
    if question.qtype == "radio" and count != 1:
        raise ValueError(f"Q{question.index}: choose one option or Other, not both.")
    if question.qtype == "checkbox":
        if count < max(question.min_select, int(question.required)):
            raise ValueError(f"Q{question.index}: at least {question.min_select} selections are required.")
        if question.max_select is not None and count > question.max_select:
            raise ValueError(f"Q{question.index}: at most {question.max_select} selections are allowed.")
        if choices and any(option in question.exclusive_options for option in choices):
            raise ValueError(f"Q{question.index}: the exclusive option must be selected alone.")
    return {"choices": choices.copy(), "other": text}


def display_value(value):
    if isinstance(value, dict) and set(value) == {"choices", "other"}:
        return "\n".join([*value["choices"], "Other: " + value["other"]])
    return "\n".join(value) if isinstance(value, list) else value


def csv_value(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
