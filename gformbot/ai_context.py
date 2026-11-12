"""Reduce oversized form prompts without discarding validation or form text."""

import json
import re
from collections import Counter

from .config import RunConfig
from .validation import is_checkbox_rule, text_rules

_OPTIONAL_FIELDS = ("description", "options", "exclusive_options", "validation", "answer_format")
_QUESTION_COLUMNS = (
    "key",
    "question",
    "type",
    "required",
    "page",
    "description",
    "options",
    "validation",
    "answer_format",
    "exclusive_options",
    "other_answer",
    "grid",
    "row",
    "unique_column",
)
_SHARED_FIELDS = set(_OPTIONAL_FIELDS) | {"other_answer", "grid", "row"}
_TABLE_GUIDANCE = (
    " Each questions.rows array has fields in questions.columns order; null and omitted cells are empty. "
    "In a column listed in questions.shared, an integer is a 1-based reference to that column's shared list. "
    "Within each options list the first option has ID 1."
)


def _signature(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def history_keys(config: RunConfig, questions: list, instructions: str = "") -> set[int]:
    """Select stable persona fields and exact dependencies for a question group."""
    keys = {q.index for q in config.questions[:6]}
    keys.update(
        q.index
        for q in config.questions
        if q.fixed_value is not None
        or q.branches
        or q.other_branch is not None
        or re.search(r"\b(?:name|email|age|gender|occupation|country|city|persona)\b", q.text, re.I)
    )
    pages = {q.page for q in questions}
    current = "\n".join(f"{q.text}\n{q.description}" for q in questions)
    directions = "\n".join(
        f"{note.get('title', '')}\n{note.get('description', '')}"
        for note in config.instructions
        if note.get("page") in pages
    )
    text = "\n".join((current, directions, config.form_description, instructions)).casefold()
    # Vague answer dependencies need the whole history. References to an image,
    # navigation or general coherence do not establish an answer dependency.
    for sentence in re.split(r"[.!?\n]", text):
        dependency = re.search(
            r"\b(?:above|earlier|previous(?:ly)?|preceding|prior)\s+(?:answers?|responses?|"
            r"selections?|choices?|values?|information|details)\b|"
            r"\b(?:answers?|responses?|selections?|choices?|values?|information|details)\s+"
            r"(?:above|earlier|previously)\b|\b(?:same as before|as above)\b|"
            r"\b(?:wrote|entered|selected|chose|said|gave)\s+(?:above|earlier|previously)\b",
            sentence,
        )
        coherence_only = re.search(r"\b(?:consistent|coherent)\b", sentence) and not re.search(
            r"\b(?:use|copy|repeat|refer|explain|derive|based|compare)\b", sentence
        )
        if dependency and not coherence_only:
            return {q.index for q in config.questions}

    ignored = {
        "fictional",
        "synthetic",
        "optional",
        "required",
        "select",
        "choose",
        "enter",
        "provide",
        "answer",
        "answers",
        "question",
        "questions",
        "response",
        "responses",
        "following",
        "including",
        "given",
        "their",
        "please",
        "these",
        "those",
        "using",
        "about",
        "which",
        "would",
        "should",
        "value",
        "values",
        "form",
        "forms",
        "rule",
        "rules",
        "control",
        "controls",
        "shared",
        "example",
        "examples",
        "standard",
        "common",
        "test",
        "tests",
    }
    titles = {
        q.index: set(re.findall(r"[^\W\d_]{4,}", q.text.casefold())) - ignored for q in config.questions
    }
    frequency = Counter(word for words in titles.values() for word in words)
    terms = set(re.findall(r"[^\W\d_]{4,}", current.casefold())) - ignored
    referenced_keys = {
        int(value)
        for value in re.findall(
            r"(?<![\w-])(?:q|(?:question|answer)(?:\s+key)?\s*#?\s*)([1-9][0-9]*)\b", text
        )
    }
    referenced_entries = set(re.findall(r"\bentry\.([a-z0-9_-]+)\b", text))
    referenced_aliases = set(re.findall(r"\b[a-z][a-z0-9]*[-_]q[0-9]+\b", text))
    for q in config.questions:
        aliases = re.findall(r"\b[a-z][a-z0-9]*[-_]q[0-9]+\b", q.text.casefold())
        explicit = q.index in referenced_keys or q.entry_id.casefold() in referenced_entries
        named = len(q.text) >= 8 and q.text.casefold() in text
        named |= bool(referenced_aliases.intersection(aliases))
        overlap = terms & titles[q.index]
        distinctive = {word for word in overlap if frequency[word] <= 8}
        if explicit or named or len(distinctive) >= 2 or any(frequency[word] <= 3 for word in overlap):
            keys.add(q.index)
    return keys


def selected_history(row: dict, keys: set[int], presets=()) -> dict:
    """Copy relevant, protected and recent answers without editing the complete local row."""
    from .answers import copy_value

    keep = {str(key) for key in keys} | {str(key) for key in presets}
    keep.update(str(key) for key in list(row)[-16:])
    return {str(key): copy_value(value) for key, value in row.items() if str(key) in keep}


def pack_history(shared: dict, profiles: dict) -> tuple[dict, dict]:
    """Pool exact prior answers across only the profiles in this request."""
    titles = shared.get("previous_questions", {})
    present = {key for profile in profiles.values() for key in profile.get("earlier_answers", {})}
    keys = dict.fromkeys(key for key in titles if key in present)
    for profile in profiles.values():
        keys.update(dict.fromkeys(profile.get("earlier_answers", {})))
    columns, indexes = [], []
    for key in keys:
        values, signatures = [], {}
        for profile in profiles.values():
            answers = profile.get("earlier_answers", {})
            if key not in answers:
                continue
            value = answers[key]
            signature = _signature(value)
            if signature not in signatures:
                values.append(value)
                signatures[signature] = len(values)
        columns.append([key, titles.get(key, ""), values])
        indexes.append(signatures)
    packed = {}
    for identity, profile in profiles.items():
        answers = profile.get("earlier_answers", {})
        packed[identity] = {
            **profile,
            "earlier_answers": [
                indexes[index][_signature(answers[key])] if key in answers else 0
                for index, key in enumerate(keys)
            ],
        }
    context = {key: value for key, value in shared.items() if key != "previous_questions"}
    context["history"] = {
        "columns": columns,
        "format": (
            "Each column is [question_key, question_title, exact_prior_values]. "
            "Each profile's earlier_answers list follows these columns. A cell is a 1-based "
            "index into that column's exact_prior_values; 0 means no prior answer. "
            "Resolve these references before using prior answers as context."
        ),
    }
    return context, packed


def _question_table(questions: list[dict]) -> dict:
    fields = dict.fromkeys(key for question in questions for key in question)
    columns = [key for key in _QUESTION_COLUMNS if key in fields]
    columns.extend(key for key in fields if key not in _QUESTION_COLUMNS)
    shared = {}
    for column in columns:
        if column not in _SHARED_FIELDS:
            continue
        signatures = [_signature(question[column]) if column in question else "" for question in questions]
        counts = Counter(signatures)
        references = {}
        values = []
        for question, signature in zip(questions, signatures, strict=True):
            if len(signature) < 12 or counts[signature] < 2:
                continue
            if signature not in references:
                values.append(question[column])
                references[signature] = len(values)
            question[column] = references[signature]
        if values:
            shared[column] = values
    rows = []
    for question in questions:
        row = [question.get(column) for column in columns]
        while row and row[-1] is None:
            row.pop()
        rows.append(row)
    return {"columns": columns, "rows": rows, "shared": shared}


def _compact_question(question: dict) -> dict:
    item = {key: value for key, value in question.items() if key not in _OPTIONAL_FIELDS or value}
    if options := item.get("options"):
        item["options"] = [options[str(index)] for index in range(1, len(options) + 1)]
    return item


def compact_https(context: dict, config: RunConfig) -> tuple[dict, str]:
    """Describe answer rules once to fit a smaller HTTPS request."""
    questions = []
    properties = context["answer_schema"]["properties"]
    for question, metadata in zip(config.questions, context["questions"], strict=True):
        item = _compact_question(metadata)
        schema = properties[item["key"]]
        raw_rules = item.pop("validation", [])
        if question.unsupported:
            item["unsupported"] = True
        elif question.qtype in ("text", "textarea"):
            try:
                rules = text_rules(question)
            except ValueError:
                item["validation"] = {"raw": raw_rules, "schema": schema}
            else:
                if rules:
                    item["validation"] = [
                        f"{rule.description} ({rule.message})" if rule.message else rule.description
                        for rule in rules
                    ]
        elif question.qtype == "checkbox" and all(is_checkbox_rule(rule) for rule in raw_rules):
            if question.min_select or question.max_select is not None:
                item["validation"] = {
                    "minimum_selections": question.min_select,
                    "maximum_selections": question.max_select,
                    **(
                        {"messages": messages}
                        if (messages := [rule[3] for rule in raw_rules if len(rule) > 3 and rule[3]])
                        else {}
                    ),
                }
        elif raw_rules:
            item["validation"] = {"raw": raw_rules, "schema": schema}
        if question.fixed_value is not None:
            item["fixed"] = schema["const"]
        questions.append(item)
    return (
        {
            **context,
            "questions": _question_table(questions),
            "answer_schema": "An object with exactly one answer for each question key in the table.",
        },
        _TABLE_GUIDANCE
        + " Answer each key. Use strings for text, dates, times and durations; use option IDs for choices. "
        "Copy fixed values exactly. Never visit a section containing an unsupported question. "
        "Follow each question's validation rules when answered, including selection limits (null means no maximum). "
        "An optional checkbox with no selections can be left empty. Lengths count UTF-16 units. "
        "Number ranges include endpoints unless the rule says outside; matching patterns apply to the whole answer. "
        "Text rules are case-sensitive. A validation object with schema supplies additional constraints.",
    )
