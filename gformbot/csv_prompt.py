"""Describe an inspected form and its accepted CSV input for a copyable prompt."""

import csv
import io
import json

from .config import QuestionConfig, RunConfig
from .generator import csv_headers
from .grids import grid_groups
from .routing import Routing
from .validation import date_format, text_schema, time_format


def _question(q: QuestionConfig, unsupported: list[str] | None) -> dict:
    column = {"question": q.text, "section": q.page, "type": q.qtype, "required": q.required}
    if q.description:
        column["description"] = q.description
    if unsupported:
        column["unsupported"] = unsupported
        return column
    if q.options:
        column["options"] = q.options
    if q.qtype == "checkbox":
        column["min_choices"] = max(q.min_select, int(q.required))
        column["max_choices"] = (
            q.max_select if q.max_select is not None else len(q.options) + int(q.allow_other)
        )
        if q.exclusive_options:
            column["choose_alone"] = q.exclusive_options
    if q.allow_other:
        column["allow_other"] = True
    if q.branches:
        column["choice_next_section"] = q.branches
    if q.other_branch is not None:
        column["other_next_section"] = q.other_branch
    if q.grid_title:
        column["grid"] = q.grid_title
        column["grid_row"] = q.grid_row
    if q.fixed_value is not None:
        column["fixed_answer"] = q.fixed_value
    if q.qtype == "date":
        column["format"] = date_format(q)
    elif q.qtype in {"time", "duration"}:
        column["format"] = time_format(q)
    elif q.qtype in {"text", "textarea"}:
        rules = text_schema(q, q.required).get("description")
        if rules:
            column["validation"] = rules
    return column


def csv_prompt(config: RunConfig) -> str:
    """Return a prompt whose CSV headers and cell formats match the local importer."""
    routes = Routing(config)
    routes.require_supported(())
    headers = csv_headers(config)
    if not headers:
        raise ValueError("The form has no answerable questions.")
    by_index = dict(zip((q.index for q in config.questions), headers, strict=True))
    form = {
        "title": config.form_title,
        "description": config.form_description,
        "sections": config.sections or [f"Section {i}" for i in routes.questions],
        "instructions": config.instructions,
        "columns": {by_index[q.index]: _question(q, routes.blockers.get(q.index)) for q in config.questions},
    }
    if routes.enabled:
        form["default_next_section"] = routes.destinations
    unique_grids = [
        [by_index[q.index] for q in rows] for rows in grid_groups(config).values() if rows[0].grid_unique
    ]
    if unique_grids:
        form["unique_grid_columns"] = unique_grids

    header = io.StringIO()
    csv.writer(header).writerow(headers)
    rules = [
        f"Create {config.count} fictional test responses for the form below.",
        "Return one UTF-8 comma-separated CSV file, or raw CSV only if you cannot attach a file. "
        "Use the exact header and order below, and one record per response. No code fences or commentary.",
        "Each row must be internally consistent. Use form descriptions, section instructions, and "
        "question descriptions as context; use these output rules for the CSV.",
        "Start at section 1. Fill required fields on the selected route. Leave every cell for a skipped "
        "question and every unanswered optional question empty. Copy any fixed answers on that route as given.",
        "Use exact choice labels, including their punctuation, and the stated date, time, and duration formats. "
        "Keep short text and Other text on a single line. Use plain text values, never spreadsheet formulas.",
    ]
    if routes.enabled:
        rules.append(
            "default_next_section lists destinations by 1-based section order. The last applicable "
            "choice_next_section or other_next_section on a section overrides its default; otherwise use "
            "the default. Destination 0 means submit. The last physical section ends the route. "
            "Do not revisit a section or enter one containing an unsupported question."
        )
    if any(q.qtype == "checkbox" for q in config.questions):
        rules.append(
            'Put checkbox labels in a JSON array inside one CSV cell, e.g. "[""First label"",""Second label""]". '
            "Select distinct labels, respect min_choices and max_choices when answered, "
            "and select a choose_alone label by itself."
        )
    if any(q.allow_other for q in config.questions):
        rules.append(
            'For an enabled Other answer, use a JSON object in one CSV cell: "{""choices"":[],'
            '""other"":""Your text""}". For checkboxes, choices may contain exact known labels; '
            "for single choice, choices must be empty. Other counts as one selection."
        )
    if unique_grids:
        rules.append(
            "Within each unique_grid_columns group, use each option label at most once per response."
        )
    rules.append("Quote cells containing commas, double quotes, or newlines; double any quote inside a cell.")
    return (
        "\n".join(rules)
        + f"\n\nExact CSV header:\n{header.getvalue().rstrip(chr(13) + chr(10))}\n\nForm data:\n"
        + json.dumps(form, ensure_ascii=False, separators=(",", ":"))
    )
