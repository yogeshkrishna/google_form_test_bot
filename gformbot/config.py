"""Form constraints and run settings, persisted without losing inspected metadata."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import math
import os
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


def validate_config(config: RunConfig) -> None:
    if config.csv_date_order not in (None, "mdy", "dmy"):
        raise ValueError("CSV date order must be mdy, dmy, or unspecified for automatic detection.")
    if not isinstance(config.url, str) or not config.url.strip():
        raise ValueError("A public Google Form URL is required.")
    if isinstance(config.max_runs, bool) or not isinstance(config.max_runs, int) or not 1 <= config.max_runs <= 500:
        raise ValueError("max_runs must be between 1 and 500.")
    if isinstance(config.count, bool) or not isinstance(config.count, int) or not 1 <= config.count <= config.max_runs:
        raise ValueError(f"Response count must be between 1 and {config.max_runs}.")
    if config.browser not in {"auto", "headless", "chrome", "firefox", "edge"}:
        raise ValueError("Browser must be auto, headless, chrome, firefox or edge.")
    if not isinstance(config.headless, bool):
        raise ValueError("headless must be true or false.")
    if not isinstance(config.background, bool):
        raise ValueError("background must be true or false.")
    for low, high in ((config.min_delay, config.max_delay), (config.page_delay_min, config.page_delay_max)):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (low, high)) or not 0 <= low <= high <= 60:
            raise ValueError("Delays must be finite numbers with 0 <= minimum <= maximum <= 60.")
    indexes = [q.index for q in config.questions]
    if any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in indexes) or len(set(indexes)) != len(indexes):
        raise ValueError("Question indexes must be distinct positive integers.")
    for q in config.questions:
        if not isinstance(q.min_select, int) or isinstance(q.min_select, bool) or q.min_select < 0:
            raise ValueError(f"Q{q.index}: min_select must be a nonnegative integer.")
        if q.max_select is not None and (isinstance(q.max_select, bool) or not isinstance(q.max_select, int) or q.max_select < max(q.min_select, int(q.required and q.qtype == "checkbox"))):
            raise ValueError(f"Q{q.index}: invalid checkbox selection bounds.")
        if len(q.options) != len(set(q.options)):
            raise ValueError(f"Q{q.index}: duplicate options are ambiguous.")
        if q.weights is not None:
            if len(q.weights) != len(q.options) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in q.weights) or not any(q.weights):
                raise ValueError(f"Q{q.index}: weights need one finite nonnegative value per option and at least one positive value.")


def config_from_schema(schema) -> RunConfig:
    questions = []
    for q in schema.questions:
        values = {name: getattr(q, name) for name in ("index", "text", "qtype", "required", "options", "page", "entry_id", "item_id", "description", "min_select", "max_select", "exclusive_options", "validation", "date_include_year", "date_include_time")}
        questions.append(QuestionConfig(**values))
    return RunConfig(url=schema.url, form_title=schema.title, questions=questions,
                     sections=schema.sections, unsupported=schema.unsupported,
                     confirmation_text=schema.confirmation_text, form_description=schema.description)


def config_to_dict(config: RunConfig) -> dict:
    value = asdict(config)
    value["questions"] = [dict(asdict(q), type=q.qtype) for q in config.questions]
    for q in value["questions"]:
        q.pop("qtype")
    setting_names = ("count", "browser", "headless", "background", "min_delay", "max_delay", "page_delay_min", "page_delay_max", "max_runs", "log_file")
    value["run_settings"] = {name: value.pop(name) for name in setting_names}
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
        parsed.append(QuestionConfig(**q))
    config = RunConfig(questions=parsed, **data)
    validate_config(config)
    return config


def save_config(config: RunConfig, path: str | Path) -> None:
    validate_config(config)
    target = clean_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(yaml.safe_dump(config_to_dict(config), allow_unicode=True, sort_keys=False), encoding="utf-8")
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
