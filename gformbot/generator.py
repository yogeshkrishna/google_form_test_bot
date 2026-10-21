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
from .validation import EMAIL_PATTERN, needs_email, normalize_csv_dates, parse_date

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
    if not isinstance(supplied, dict):
        raise ValueError("Column mapping must be an object.")
    if not isinstance(headers, list) or any(not isinstance(header, str) for header in headers):
        raise ValueError("CSV column names must be text.")
    result = {}
    used = {}
    known = {str(q.index) for q in config.questions}
    mapped_indexes = set()
    for key in supplied:
        if str(key) not in known:
            raise ValueError(f"Unknown question in column mapping: {key}")
        if str(key) in mapped_indexes:
            raise ValueError(f"Q{key}: duplicate column mapping keys.")
        mapped_indexes.add(str(key))
    by_name = {}
    for header in headers:
        by_name.setdefault(_normal(header), []).append(header)
    for q in config.questions:
        explicit = q.index in supplied or str(q.index) in supplied or q.pool_column is not None
        if explicit:
            column = supplied.get(q.index, supplied.get(str(q.index), q.pool_column))
            if column is not None and column not in headers:
                raise ValueError(f"Q{q.index}: CSV column {column!r} was not found.")
        else:
            aliases = {_normal(q.text), f"q{q.index}"}
            if q.entry_id:
                aliases.add(_normal(f"entry.{q.entry_id}"))
            matches = [header for alias in aliases for header in by_name.get(alias, ())]
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


def mapping_diagnostics(config: RunConfig, headers: list[str], mapping: dict | None = None) -> dict:
    """Describe accepted mappings so callers can make blank/dropped data explicit."""
    columns = map_columns(config, headers, mapping)
    used = {column for column in columns.values() if column is not None}
    return {
        "mapping": columns,
        "unmatched_questions": [index for index, column in columns.items() if column is None],
        "unused_columns": [header for header in headers if header not in used],
        "matched_count": len(used),
    }


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
                raise ValueError(
                    f"Q{q.index}: invalid checkbox JSON; use an array of exact option labels."
                ) from exc
            if not isinstance(selected, list):
                raise ValueError(f"Q{q.index}: checkbox answer must be a JSON array.")
        else:
            delimiter = "|" if "|" in raw else ","
            # Build reachable token boundaries once, then retain at most two
            # interpretations per boundary. Linked tails avoid quadratic copies
            # and an explicit stack avoids recursion limits on long CSV cells.
            edges = {}
            pending = [0]
            while pending:
                start = pending.pop()
                if start in edges:
                    continue
                matches = []
                for option in q.options:
                    if not raw.startswith(option, start):
                        continue
                    end = start + len(option)
                    if end == len(raw):
                        matches.append((option, end))
                    else:
                        while end < len(raw) and raw[end].isspace():
                            end += 1
                        if end == len(raw) or raw[end] != delimiter:
                            continue
                        end += 1
                        while end < len(raw) and raw[end].isspace():
                            end += 1
                        if end < len(raw):
                            matches.append((option, end))
                            if end not in edges:
                                pending.append(end)
                edges[start] = matches
            paths = {len(raw): [None]}
            for start in sorted(edges, reverse=True):
                matches = []
                for option, end in edges[start]:
                    for tail in paths.get(end, ()):
                        matches.append((option, tail))
                        if len(matches) == 2:
                            break
                    if len(matches) == 2:
                        break
                paths[start] = matches
            interpretations = paths[0]
            if len(interpretations) != 1:
                raise ValueError(
                    f"Q{q.index}: unknown or ambiguous checkbox labels. Store exact selections as a JSON array."
                )
            selected, tail = [], interpretations[0]
            while tail is not None:
                option, tail = tail
                selected.append(option)
    else:
        raise ValueError(f"Q{q.index}: checkbox answer must be a list or CSV string.")
    if any(not isinstance(v, str) or v not in q.options for v in selected):
        raise ValueError(f"Q{q.index}: checkbox answer contains an unknown option.")
    return selected


def validate_answer(
    config: RunConfig, answer: dict, *, repair: bool = False, changes: list[str] | None = None
) -> dict[int, Any]:
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
                value = value[: q.max_select]
                changes.append(f"Q{q.index}: capped selections at {q.max_select}")
            minimum = max(q.min_select, int(q.required))
            if (value or q.required) and len(value) < minimum:
                raise ValueError(f"{label}: at least {minimum} selections are required.")
        elif q.qtype in CHOICE_TYPES | TEXT_TYPES:
            value = "" if value is None else value
            if not isinstance(value, str):
                raise ValueError(f"{label}: answer must be text.")
            if q.qtype == "text" and ("\r" in value or "\n" in value):
                hint = " Enter a valid email address." if needs_email(q) else ""
                raise ValueError(f"{label}: single-line answers cannot contain line breaks.{hint}")
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


def load_reference(
    config: RunConfig,
    path: str | Path,
    mapping: dict | None = None,
    repair: bool = False,
    changes: list[str] | None = None,
) -> list[dict[int, Any]]:
    validate_config(config)
    headers, rows = read_pool(path)
    columns = map_columns(config, headers, mapping)
    if (
        not any(column is not None for column in columns.values())
        and mapping is None
        and not config.pool_column_map
    ):
        raise ValueError(
            "No CSV columns match the form. Map columns explicitly before importing blank responses."
        )
    for question in config.questions:
        column = columns[question.index]
        if question.qtype == "date" and column:
            normalized = normalize_csv_dates(question, [row[column] for row in rows], config.csv_date_order)
            for row, value in zip(rows, normalized, strict=True):
                row[column] = value
    result = []
    for line, row in enumerate(rows, 2):
        local_changes = []
        try:
            result.append(
                validate_answer(
                    config,
                    {q.index: row[columns[q.index]] if columns[q.index] else "" for q in config.questions},
                    repair=repair,
                    changes=local_changes,
                )
            )
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
    available = [o for o, w in zip(q.options, weights, strict=True) if w > 0]
    exclusive = set(q.exclusive_options)
    regular = [o for o in available if o not in exclusive]
    minimum = max(q.min_select, int(q.required))
    maximum = min(q.max_select if q.max_select is not None else len(available), len(available))
    feasible = [n for n in range(minimum, maximum + 1) if n < 2 or n <= len(regular)]
    if not feasible:
        raise ValueError(f"Q{q.index}: weights and checkbox bounds cannot produce valid answers.")
    count = random.choice(feasible)
    remaining = available[:] if count < 2 else regular[:]
    weight_by_option = dict(zip(q.options, weights, strict=True))
    selected = []
    for _ in range(count):
        option = random.choices(remaining, weights=[weight_by_option[o] for o in remaining], k=1)[0]
        remaining.remove(option)
        selected.append(option)
    return selected


def make_answer(config: RunConfig, pool_row: dict | None = None) -> dict[int, Any]:
    if pool_row is not None:
        mapping = map_columns(config, list(pool_row))
        return validate_answer(
            config,
            {q.index: pool_row[mapping[q.index]] if mapping[q.index] else "" for q in config.questions},
        )
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


def prepare_responses(
    config: RunConfig,
    profiles: list[dict[int, Any]] | None = None,
    *,
    ordered: bool = False,
    sampling: str | None = None,
) -> list[dict[int, Any]]:
    validate_config(config)
    if sampling is None:
        sampling = "ordered" if ordered else "sample"
    if sampling not in {"ordered", "shuffle", "sample"}:
        raise ValueError("Sampling must be ordered, shuffle, or sample.")
    if ordered and sampling != "ordered":
        raise ValueError("Choose ordered mode or another sampling mode, not both.")
    if profiles is None and config.pool_file:
        profiles = load_reference(config, config.pool_file)
    if profiles is not None:
        if not profiles:
            raise ValueError("The response pool is empty.")
        profiles = [validate_answer(config, row) for row in profiles]
        if sampling in {"ordered", "shuffle"} and config.count > len(profiles):
            raise ValueError(
                f"{sampling.capitalize()} mode needs {config.count} rows; the CSV has {len(profiles)}."
            )
        chosen = (
            profiles[: config.count]
            if sampling == "ordered"
            else random.sample(profiles, k=config.count)
            if sampling == "shuffle"
            else random.choices(profiles, k=config.count)
        )
        return [
            {key: value.copy() if isinstance(value, list) else value for key, value in row.items()}
            for row in chosen
        ]
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
            writer.writerow(
                [
                    json.dumps(row[q.index], ensure_ascii=False)
                    if isinstance(row[q.index], list)
                    else row[q.index]
                    for q in config.questions
                ]
            )
    temporary.replace(target)
    return target


def answer_stream(config: RunConfig):
    profiles = load_reference(config, config.pool_file) if config.pool_file else None
    while True:
        if profiles:
            yield {
                key: value.copy() if isinstance(value, list) else value
                for key, value in random.choice(profiles).items()
            }
        else:
            yield make_answer(config)
