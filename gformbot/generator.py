"""Prepare and validate complete response profiles before opening a browser."""
from __future__ import annotations

import csv
import json
import random
import re
import unicodedata
from pathlib import Path
from typing import Any

from .config import QuestionConfig, RunConfig, clean_path, validate_config
from .validation import EMAIL_PATTERN, needs_email, parse_date, normalize_csv_dates

CHOICE_TYPES = {"radio", "checkbox", "dropdown", "scale"}
TEXT_TYPES = {"text", "textarea", "date"}


class CheckboxRepairError(ValueError):
    """An imported answer has one of the explicitly repairable checkbox issues."""


def _normal(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def read_pool(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    target = clean_path(path)
    if target.suffix.lower() != ".csv":
        raise ValueError("Choose a .csv file. Export your spreadsheet as CSV first.")
    try:
        with target.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            if not headers or any(not h.strip() for h in headers) or len(set(headers)) != len(headers):
                raise ValueError("CSV headers must be nonempty and distinct.")
            rows = []
            for line, row in enumerate(reader, 2):
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(f"CSV row {line} has a different number of cells than its header.")
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Cannot read CSV {target}: {exc}") from exc
    if not rows:
        raise ValueError("The CSV contains no response rows.")
    return list(headers), rows


def load_pool(path: str | Path) -> list[dict[str, str]]:
    return read_pool(path)[1]


def map_columns(config: RunConfig, headers: list[str], mapping: dict | None = None) -> dict[int, str | None]:
    supplied = config.pool_column_map if mapping is None else mapping
    result = {}
    used = {}
    known = {q.index for q in config.questions}
    for key in supplied:
        if str(key) not in {str(i) for i in known}:
            raise ValueError(f"Unknown question in column mapping: {key}")
    for q in config.questions:
        explicit = q.index in supplied or str(q.index) in supplied or q.pool_column is not None
        if explicit:
            column = supplied.get(q.index, supplied.get(str(q.index), q.pool_column))
            if column is not None and column not in headers:
                raise ValueError(f"Q{q.index}: CSV column {column!r} was not found.")
        else:
            aliases = {_normal(q.text), f"q{q.index}"}
            if q.entry_id:
                aliases.add(f"entry.{q.entry_id}")
            matches = [h for h in headers if _normal(h) in aliases]
            if len(matches) > 1:
                raise ValueError(f"Q{q.index}: multiple CSV columns match. Choose its source column.")
            column = matches[0] if matches else None
        if column is None and q.required:
            raise ValueError(f"Q{q.index}: missing required CSV column for {q.text!r}.")
        if column is not None:
            if column in used:
                raise ValueError(f"Column {column!r} maps to both Q{used[column]} and Q{q.index}.")
            used[column] = q.index
        result[q.index] = column
    return result


def parse_checkbox(q: QuestionConfig, value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        selected = value[:]
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                selected = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"Q{q.index}: invalid checkbox JSON; use an array of exact option labels.") from exc
            if not isinstance(selected, list):
                raise ValueError(f"Q{q.index}: checkbox answer must be a JSON array.")
        else:
            delimiter = "|" if "|" in raw else ","
            cache = {}

            def segment(start):
                if start in cache:
                    return cache[start]
                matches = []
                for option in q.options:
                    if not raw.startswith(option, start):
                        continue
                    end = start + len(option)
                    if end == len(raw):
                        matches.append([option])
                    else:
                        while end < len(raw) and raw[end].isspace():
                            end += 1
                        if end == len(raw) or raw[end] != delimiter:
                            continue
                        end += 1
                        while end < len(raw) and raw[end].isspace():
                            end += 1
                        for suffix in segment(end):
                            matches.append([option] + suffix)
                            if len(matches) >= 2:
                                cache[start] = matches[:2]
                                return cache[start]
                cache[start] = matches[:2]
                return cache[start]

            interpretations = segment(0)
            if len(interpretations) != 1:
                raise ValueError(f"Q{q.index}: unknown or ambiguous checkbox labels. Store exact selections as a JSON array.")
            selected = interpretations[0]
    else:
        raise ValueError(f"Q{q.index}: checkbox answer must be a list or CSV string.")
    if any(not isinstance(v, str) or v not in q.options for v in selected):
        raise ValueError(f"Q{q.index}: checkbox answer contains an unknown option.")
    return selected


def validate_answer(config: RunConfig, answer: dict, *, repair: bool = False, changes: list[str] | None = None) -> dict[int, Any]:
    if config.unsupported:
        raise ValueError("Unsupported form: " + "; ".join(config.unsupported))
    if not config.questions:
        raise ValueError("The form has no answerable questions.")
    if not isinstance(answer, dict):
        raise ValueError("Each response must be an object.")
    allowed = {str(q.index) for q in config.questions}
    if any(str(key) not in allowed for key in answer):
        raise ValueError("Response contains unknown question indexes.")
    result = {}
    changes = changes if changes is not None else []
    for q in config.questions:
        if q.index in answer and str(q.index) in answer:
            raise ValueError(f"Q{q.index}: duplicate response keys.")
        value = answer.get(q.index, answer.get(str(q.index), ""))
        label = f"Q{q.index} ({q.text})"
        if q.qtype == "checkbox":
            value = parse_checkbox(q, value)
            if len(value) != len(set(value)):
                if not repair:
                    raise CheckboxRepairError(f"{label}: duplicate checkbox selections.")
                value = list(dict.fromkeys(value))
                changes.append(f"Q{q.index}: removed duplicate selections")
            exclusive = [v for v in value if v in q.exclusive_options]
            if exclusive and len(value) > 1:
                if not repair:
                    raise CheckboxRepairError(f"{label}: {exclusive[0]!r} must be selected alone.")
                value = [exclusive[0]]
                changes.append(f"Q{q.index}: kept exclusive option alone")
            if q.max_select is not None and len(value) > q.max_select:
                if not repair:
                    raise CheckboxRepairError(f"{label}: at most {q.max_select} selections are allowed.")
                value = value[:q.max_select]
                changes.append(f"Q{q.index}: capped selections at {q.max_select}")
            minimum = max(q.min_select, int(q.required))
            if (value or q.required) and len(value) < minimum:
                raise ValueError(f"{label}: at least {minimum} selections are required.")
        elif q.qtype in CHOICE_TYPES | TEXT_TYPES:
            value = "" if value is None else value
            if not isinstance(value, str):
                raise ValueError(f"{label}: answer must be text.")
            if not value.strip():
                if q.required:
                    raise ValueError(f"{label}: a required answer is blank.")
                value = ""
            elif q.qtype in CHOICE_TYPES and value not in q.options:
                raise ValueError(f"{label}: {value!r} is not an exact option.")
            if value and q.qtype == "date":
                value, _ = parse_date(q, value)
            if value and needs_email(q) and not re.fullmatch(EMAIL_PATTERN, value):
                raise ValueError(f"{label}: enter a valid email address.")
        else:
            raise ValueError(f"{label}: unsupported question type {q.qtype!r}.")
        result[q.index] = value
    return result


def load_reference(config: RunConfig, path: str | Path, mapping: dict | None = None, repair: bool = False, changes: list[str] | None = None) -> list[dict[int, Any]]:
    headers, rows = read_pool(path)
    columns = map_columns(config, headers, mapping)
    for question in config.questions:
        column = columns[question.index]
        if question.qtype == "date" and column:
            normalized = normalize_csv_dates(question, [row[column] for row in rows], config.csv_date_order)
            for row, value in zip(rows, normalized):
                row[column] = value
    result = []
    for line, row in enumerate(rows, 2):
        local_changes = []
        try:
            result.append(validate_answer(config, {q.index: row[columns[q.index]] if columns[q.index] else "" for q in config.questions}, repair=repair, changes=local_changes))
        except ValueError as exc:
            raise type(exc)(f"CSV row {line}: {exc}") from exc
        if changes is not None:
            changes.extend(f"Row {line}: {change}" for change in local_changes)
    return result


def apply_reference_weights(config: RunConfig, profiles: list[dict[int, Any]]) -> None:
    for q in config.questions:
        if q.qtype not in CHOICE_TYPES:
            continue
        counts = {o: 0 for o in q.options}
        for profile in profiles:
            value = profile[q.index]
            for option in value if isinstance(value, list) else [value]:
                if option in counts:
                    counts[option] += 1
        if any(counts.values()):
            q.weights = [counts[o] for o in q.options]


def _random_from_options(q: QuestionConfig) -> Any:
    if not q.options:
        raise ValueError(f"Q{q.index}: no answer options were found.")
    weights = q.weights or [1] * len(q.options)
    if q.qtype != "checkbox":
        return random.choices(q.options, weights=weights, k=1)[0]
    available = [o for o, w in zip(q.options, weights) if w > 0]
    regular = [o for o in available if o not in q.exclusive_options]
    minimum = max(q.min_select, int(q.required))
    maximum = min(q.max_select if q.max_select is not None else max(3, minimum), len(available))
    feasible = [n for n in range(minimum, maximum + 1) if n < 2 or n <= len(regular)]
    if not feasible:
        raise ValueError(f"Q{q.index}: weights and checkbox bounds cannot produce valid answers.")
    count = random.choice(feasible)
    remaining = available[:] if count < 2 else regular[:]
    selected = []
    for _ in range(count):
        option = random.choices(remaining, weights=[weights[q.options.index(o)] for o in remaining], k=1)[0]
        remaining.remove(option)
        selected.append(option)
    return selected


def make_answer(config: RunConfig, pool_row: dict | None = None) -> dict[int, Any]:
    if pool_row is not None:
        mapping = map_columns(config, list(pool_row))
        return validate_answer(config, {q.index: pool_row[mapping[q.index]] if mapping[q.index] else "" for q in config.questions})
    answer = {}
    for q in config.questions:
        if q.fixed_value is not None:
            value = q.fixed_value
        elif q.qtype in CHOICE_TYPES:
            value = _random_from_options(q)
        elif q.text_pool:
            value = random.choice(q.text_pool)
        else:
            value = ""
        answer[q.index] = value
    return validate_answer(config, answer)


def prepare_responses(config: RunConfig, profiles: list[dict[int, Any]] | None = None, *, ordered: bool = False) -> list[dict[int, Any]]:
    validate_config(config)
    if profiles is None and config.pool_file:
        profiles = load_reference(config, config.pool_file)
    if profiles is not None:
        if not profiles:
            raise ValueError("The response pool is empty.")
        profiles = [validate_answer(config, row) for row in profiles]
        if ordered and config.count > len(profiles):
            raise ValueError(f"Ordered mode needs {config.count} rows; the CSV has {len(profiles)}.")
        chosen = profiles[:config.count] if ordered else random.choices(profiles, k=config.count)
        return [dict(row) for row in chosen]
    return [make_answer(config) for _ in range(config.count)]


def write_csv(config: RunConfig, answers: list[dict], path: str | Path) -> Path:
    target = clean_path(path)
    if config.pool_file and target.resolve() == clean_path(config.pool_file).resolve():
        raise ValueError("The output CSV cannot overwrite your source CSV.")
    if target.exists():
        raise ValueError(f"Output already exists: {target}. Choose a different filename.")
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = [f"entry.{q.entry_id}" if q.entry_id else f"Q{q.index}" for q in config.questions]
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for answer in answers:
            row = validate_answer(config, answer)
            writer.writerow([json.dumps(row[q.index], ensure_ascii=False) if isinstance(row[q.index], list) else row[q.index] for q in config.questions])
    temporary.replace(target)
    return target


def answer_stream(config: RunConfig):
    profiles = load_reference(config, config.pool_file) if config.pool_file else None
    while True:
        yield dict(random.choice(profiles)) if profiles else make_answer(config)
