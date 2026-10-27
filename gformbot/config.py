"""Form constraints and run settings, persisted without losing inspected metadata."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "gformbot_config.yaml"


def clean_path(value: str | Path) -> Path:
    raw = str(value).strip()
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return Path(os.path.expandvars(raw)).expanduser()


@dataclass
class QuestionConfig:
    index: int
    text: str
    qtype: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    page: int = 1
    fixed_value: Any = None
    weights: list[float] | None = None
    max_select: int | None = None
    pool_column: str | None = None
    text_pool: list[str] | None = None
    entry_id: str = ""
    item_id: str = ""
    description: str = ""
    min_select: int = 0
    exclusive_options: list[str] = field(default_factory=list)
    validation: list = field(default_factory=list)
    date_include_year: bool = True
    date_include_time: bool = False
    branches: dict[str, int] = field(default_factory=dict)
    grid_title: str = ""
    grid_row: str = ""
    grid_unique: bool = False
    unsupported: list[str] = field(default_factory=list)
    rating_icon: str = ""


@dataclass
class RunConfig:
    url: str
    form_title: str = "Google Form"
    pool_file: str | None = None
    pool_column_map: dict = field(default_factory=dict)
    csv_date_order: str | None = None
    questions: list[QuestionConfig] = field(default_factory=list)
    count: int = 1
    browser: str = "headless"
    headless: bool = True
    background: bool = False
    min_delay: float = 0.25
    max_delay: float = 0.75
    page_delay_min: float = 0.0
    page_delay_max: float = 0.0
    max_runs: int = 500
    log_file: str = "gformbot-runs.csv"
    sections: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    confirmation_text: str = ""
    form_description: str = ""
    instructions: list[dict] = field(default_factory=list)
    requires_signin: bool = False
    one_response: bool | None = None
    email_collection: str = "unknown"
    section_ids: list[str] = field(default_factory=list)
    section_next: list[int] = field(default_factory=list)


def validate_config(config: RunConfig) -> None:
    if not isinstance(config, RunConfig):
        raise ValueError("Configuration must be a RunConfig object.")
    if not isinstance(config.requires_signin, bool):
        raise ValueError("requires_signin must be true or false.")
    if config.one_response is not None and type(config.one_response) is not bool:
        raise ValueError("one_response must be true, false, or unknown.")
    if config.email_collection not in ("unknown", "none", "verified", "input"):
        raise ValueError("Unrecognized email collection setting.")
    if config.csv_date_order not in (None, "mdy", "dmy"):
        raise ValueError("CSV date order must be mdy, dmy, or unspecified for automatic detection.")
    if not isinstance(config.url, str) or not config.url.strip():
        raise ValueError("A public Google Form URL is required.")
    if (
        isinstance(config.max_runs, bool)
        or not isinstance(config.max_runs, int)
        or not 1 <= config.max_runs <= 500
    ):
        raise ValueError("max_runs must be between 1 and 500.")
    if (
        isinstance(config.count, bool)
        or not isinstance(config.count, int)
        or not 1 <= config.count <= config.max_runs
    ):
        raise ValueError(f"Response count must be between 1 and {config.max_runs}.")
    if not isinstance(config.browser, str) or config.browser not in {
        "auto",
        "headless",
        "chrome",
        "firefox",
        "edge",
    }:
        raise ValueError("Browser must be auto, headless, chrome, firefox or edge.")
    if not isinstance(config.headless, bool):
        raise ValueError("headless must be true or false.")
    if not isinstance(config.background, bool):
        raise ValueError("background must be true or false.")
    for name in ("form_title", "form_description", "confirmation_text"):
        if not isinstance(getattr(config, name), str):
            raise ValueError(f"{name} must be text.")
    for name in ("pool_file", "log_file"):
        value = getattr(config, name)
        if value is not None and (not isinstance(value, (str, os.PathLike)) or not str(value).strip()):
            raise ValueError(f"{name} must be a nonempty file path.")
    if config.log_file is None:
        raise ValueError("log_file must be a nonempty file path.")
    for name in ("sections", "unsupported"):
        value = getattr(config, name)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{name} must be a list of text values.")
    if not isinstance(config.pool_column_map, dict):
        raise ValueError("pool_column_map must be an object mapping question indexes to columns.")
    if not isinstance(config.questions, list) or any(
        not isinstance(q, QuestionConfig) for q in config.questions
    ):
        raise ValueError("questions must be a list of QuestionConfig objects.")
    if not isinstance(config.instructions, list):
        raise ValueError("instructions must be a list of form instruction objects.")
    for note in config.instructions:
        if (
            not isinstance(note, dict)
            or set(note) != {"page", "before_question", "title", "description"}
            or type(note["page"]) is not int
            or note["page"] < 1
            or type(note["before_question"]) is not int
            or note["before_question"] < 1
            or not isinstance(note["title"], str)
            or not isinstance(note["description"], str)
        ):
            raise ValueError("Each instruction needs page, before_question, title, and description.")
    for low, high in ((config.min_delay, config.max_delay), (config.page_delay_min, config.page_delay_max)):
        if (
            any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (low, high)
            )
            or not 0 <= low <= high <= 60
        ):
            raise ValueError("Delays must be finite numbers with 0 <= minimum <= maximum <= 60.")
    indexes = [q.index for q in config.questions]
    if any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in indexes) or len(
        set(indexes)
    ) != len(indexes):
        raise ValueError("Question indexes must be distinct positive integers.")
    known_indexes = {str(index) for index in indexes}
    for key, column in config.pool_column_map.items():
        if isinstance(key, bool) or str(key) not in known_indexes:
            raise ValueError(f"Unknown question in column mapping: {key!r}.")
        if column is not None and (not isinstance(column, str) or not column.strip()):
            raise ValueError(f"Q{key}: source column must be text or null.")
    for q in config.questions:
        label = f"Q{q.index}"
        if not isinstance(q.text, str) or not isinstance(q.description, str):
            raise ValueError(f"{label}: question and description must be text.")
        if not isinstance(q.qtype, str) or (
            q.qtype
            not in {
                "text",
                "textarea",
                "date",
                "time",
                "duration",
                "radio",
                "checkbox",
                "dropdown",
                "scale",
                "rating",
            }
            and not config.unsupported
        ):
            raise ValueError(f"{label}: unsupported question type {q.qtype!r}.")
        if type(q.page) is not int or q.page < 1:
            raise ValueError(f"{label}: page must be a positive integer.")
        for name in ("required", "date_include_year", "date_include_time"):
            if not isinstance(getattr(q, name), bool):
                raise ValueError(f"{label}: {name} must be true or false.")
        for name in ("entry_id", "item_id"):
            if not isinstance(getattr(q, name), str):
                raise ValueError(f"{label}: {name} must be text.")
        if q.pool_column is not None and (not isinstance(q.pool_column, str) or not q.pool_column.strip()):
            raise ValueError(f"{label}: pool_column must be nonempty text or null.")
        if not isinstance(q.options, list) or any(
            not isinstance(option, str) or not option.strip() and not q.unsupported for option in q.options
        ):
            raise ValueError(f"{label}: options must be a list of nonempty text labels.")
        if q.qtype in {"radio", "checkbox", "dropdown", "scale"} and not q.options and not config.unsupported:
            raise ValueError(f"{label}: choice questions need at least one option.")
        if (
            q.qtype == "rating"
            and not q.unsupported
            and (
                q.rating_icon not in ("star", "heart", "thumb_up")
                or not 3 <= len(q.options) <= 10
                or q.options != [str(i) for i in range(1, len(q.options) + 1)]
            )
        ):
            raise ValueError(f"{label}: invalid rating values or icon.")
        if (
            not isinstance(q.exclusive_options, list)
            or any(not isinstance(option, str) or option not in q.options for option in q.exclusive_options)
            or len(set(q.exclusive_options)) != len(q.exclusive_options)
        ):
            raise ValueError(f"{label}: exclusive_options must contain distinct known option labels.")
        if not isinstance(q.validation, list) or any(not isinstance(rule, list) for rule in q.validation):
            raise ValueError(f"{label}: validation must be a list of rules.")
        if not isinstance(q.unsupported, list) or any(
            not isinstance(issue, str) or not issue for issue in q.unsupported
        ):
            raise ValueError(f"{label}: unsupported findings must be nonempty text values.")
        if q.qtype in ("text", "textarea") and not config.unsupported:
            from .validation import text_rules

            text_rules(q)
        if not isinstance(q.min_select, int) or isinstance(q.min_select, bool) or q.min_select < 0:
            raise ValueError(f"Q{q.index}: min_select must be a nonnegative integer.")
        if q.max_select is not None and (
            isinstance(q.max_select, bool)
            or not isinstance(q.max_select, int)
            or q.max_select < max(q.min_select, int(q.required and q.qtype == "checkbox"))
        ):
            raise ValueError(f"Q{q.index}: invalid checkbox selection bounds.")
        if len(q.options) != len(set(q.options)) and not q.unsupported:
            raise ValueError(f"Q{q.index}: duplicate options are ambiguous.")
        if q.weights is not None:
            if (
                not isinstance(q.weights, list)
                or len(q.weights) != len(q.options)
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                    for v in q.weights
                )
                or not any(q.weights)
            ):
                raise ValueError(
                    f"Q{q.index}: weights need one finite nonnegative value per option and at least one positive value."
                )
        if q.text_pool is not None and (
            not isinstance(q.text_pool, list) or any(not isinstance(text, str) for text in q.text_pool)
        ):
            raise ValueError(f"{label}: text_pool must be a list of text values.")
        if q.fixed_value is not None or q.text_pool:
            # Keep config import lightweight; only configured generated values
            # need the answer validator, which deliberately does not recurse.
            from .generator import validate_answer

            single = RunConfig(config.url, questions=[replace(q, page=1, branches={})])
            values = [q.fixed_value] if q.fixed_value is not None else q.text_pool
            for value in values:
                validate_answer(single, {q.index: value})
    from .grids import grid_groups
    from .routing import Routing

    grid_groups(config)
    Routing(config)


def config_from_schema(schema) -> RunConfig:
    questions = []
    for q in schema.questions:
        values = {
            name: getattr(q, name)
            for name in (
                "index",
                "text",
                "qtype",
                "required",
                "options",
                "page",
                "entry_id",
                "item_id",
                "description",
                "min_select",
                "max_select",
                "exclusive_options",
                "validation",
                "date_include_year",
                "date_include_time",
                "branches",
                "grid_title",
                "grid_row",
                "grid_unique",
                "unsupported",
                "rating_icon",
            )
        }
        questions.append(QuestionConfig(**values))
    return RunConfig(
        url=schema.url,
        form_title=schema.title,
        questions=questions,
        sections=schema.sections,
        unsupported=schema.unsupported,
        confirmation_text=schema.confirmation_text,
        form_description=schema.description,
        instructions=getattr(schema, "instructions", []),
        requires_signin=getattr(schema, "requires_signin", False),
        one_response=getattr(schema, "one_response", None),
        email_collection=getattr(schema, "email_collection", "unknown"),
        section_ids=getattr(schema, "section_ids", []),
        section_next=getattr(schema, "section_next", []),
    )


def config_to_dict(config: RunConfig) -> dict:
    value = asdict(config)
    # Accepted by older callers/config files, but these settings never affected
    # execution. Do not advertise inert knobs in newly written configuration.
    value.pop("page_delay_min")
    value.pop("page_delay_max")
    for q in value["questions"]:
        q["type"] = q.pop("qtype")
    setting_names = (
        "count",
        "browser",
        "headless",
        "background",
        "min_delay",
        "max_delay",
        "max_runs",
        "log_file",
    )
    value["run_settings"] = {name: value.pop(name) for name in setting_names}
    for name in ("pool_file",):
        if value[name] is not None:
            value[name] = os.fspath(value[name])
    value["run_settings"]["log_file"] = os.fspath(value["run_settings"]["log_file"])
    return value


def config_from_dict(value: dict) -> RunConfig:
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object.")
    data = dict(value)
    settings = data.pop("run_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("run_settings must be an object.")
    data.update(settings)
    questions = data.pop("questions", [])
    if not isinstance(questions, list):
        raise ValueError("questions must be a list.")
    parsed = []
    allowed_q = {f.name for f in fields(QuestionConfig)}
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("Each question must be an object.")
        q = dict(question)
        q["qtype"] = q.pop("type", q.get("qtype", "unknown"))
        unknown = set(q) - allowed_q
        if unknown:
            raise ValueError(f"Unknown question settings: {sorted(unknown)}")
        try:
            parsed.append(QuestionConfig(**q))
        except TypeError as exc:
            raise ValueError(f"Invalid question configuration: {exc}") from exc
    unknown = set(data) - {f.name for f in fields(RunConfig)}
    if unknown:
        raise ValueError(f"Unknown configuration settings: {sorted(unknown)}")
    try:
        config = RunConfig(questions=parsed, **data)
    except TypeError as exc:
        raise ValueError(f"Invalid configuration: {exc}") from exc
    validate_config(config)
    return config


def save_config(config: RunConfig, path: str | Path) -> None:
    validate_config(config)
    target = clean_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        yaml.safe_dump(config_to_dict(config), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(target)


def load_config(path: str | Path) -> RunConfig:
    target = clean_path(path).resolve()
    try:
        config = config_from_dict(yaml.safe_load(target.read_text(encoding="utf-8-sig")))
    except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot read config {target}: {exc}") from exc
    for name in ("pool_file", "log_file"):
        value = getattr(config, name)
        if value:
            p = clean_path(value)
            setattr(config, name, str(p if p.is_absolute() else target.parent / p))
    return config
