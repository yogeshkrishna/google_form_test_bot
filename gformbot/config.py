"""
Config schema — the YAML/JSON config file that describes a form run.

A config file is automatically generated from `gformbot inspect <url>` and
stored in the current directory as `gformbot_config.yaml`.

Users edit the config to:
  - Set their response pool CSV (or leave blank for pure synthetic)
  - Set per-question weights / biases
  - Set run settings (count, delay, browser, headless)
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


DEFAULT_CONFIG_NAME = "gformbot_config.yaml"


@dataclass
class QuestionConfig:
    index: int
    text: str
    qtype: str                          # radio | checkbox | text | textarea | dropdown
    required: bool = False
    options: list[str] = field(default_factory=list)
    page: int = 1
    # Optional override: fixed value(s) to always use. None = draw from pool/random.
    fixed_value: Optional[Any] = None
    # Weights for random selection (parallel list to options). None = uniform.
    weights: Optional[list[float]] = None
    # Max selections for checkbox questions. None = no limit (up to all).
    max_select: Optional[int] = None
    # Pool column name override. None = auto-match by question text.
    pool_column: Optional[str] = None
    # For text fields: a list of strings to randomly pick from. None = use pool or empty.
    text_pool: Optional[list[str]] = None


@dataclass
class RunConfig:
    url: str
    form_title: str = "Google Form"

    # ---- response pool ----
    pool_file: Optional[str] = None          # path to CSV; None = synthetic only
    pool_column_map: dict = field(default_factory=dict)  # {q_index: csv_column_name}

    # ---- questions ----
    questions: list[QuestionConfig] = field(default_factory=list)

    # ---- run settings ----
    count: int = 1
    browser: str = "auto"                   # auto | chrome | firefox | edge | headless
    headless: bool = False
    min_delay: float = 0.25
    max_delay: float = 0.75
    page_delay_min: float = 0.8
    page_delay_max: float = 1.4
    max_runs: int = 500

    # ---- logging ----
    log_file: str = "gformbot_log.csv"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_config(config: RunConfig, path: Path) -> None:
    data = _config_to_dict(config)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_config(path: Path) -> RunConfig:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _dict_to_config(data)


def config_from_schema(schema) -> RunConfig:
    """Build a default RunConfig from an inspected FormSchema."""
    from gformbot.inspector import FormSchema
    questions = []
    for q in schema.questions:
        qc = QuestionConfig(
            index=q.index,
            text=q.text,
            qtype=q.qtype,
            required=q.required,
            options=q.options,
            page=q.page,
        )
        # Sensible defaults for checkboxes
        if q.qtype == "checkbox" and q.options:
            qc.max_select = min(3, len(q.options) - 1)
        questions.append(qc)

    return RunConfig(
        url=schema.url,
        form_title=schema.title,
        questions=questions,
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _config_to_dict(c: RunConfig) -> dict:
    d = {
        "url": c.url,
        "form_title": c.form_title,
        "pool_file": c.pool_file,
        "run_settings": {
            "count": c.count,
            "browser": c.browser,
            "headless": c.headless,
            "min_delay": c.min_delay,
            "max_delay": c.max_delay,
            "page_delay_min": c.page_delay_min,
            "page_delay_max": c.page_delay_max,
            "max_runs": c.max_runs,
            "log_file": c.log_file,
        },
        "questions": [_qc_to_dict(q) for q in c.questions],
    }
    return d


def _qc_to_dict(q: QuestionConfig) -> dict:
    d: dict = {
        "index": q.index,
        "text": q.text,
        "type": q.qtype,
        "page": q.page,
        "required": q.required,
    }
    if q.options:
        d["options"] = q.options
    if q.weights:
        d["weights"] = q.weights
    if q.fixed_value is not None:
        d["fixed_value"] = q.fixed_value
    if q.max_select is not None:
        d["max_select"] = q.max_select
    if q.pool_column:
        d["pool_column"] = q.pool_column
    if q.text_pool:
        d["text_pool"] = q.text_pool
    return d


def _dict_to_config(d: dict) -> RunConfig:
    rs = d.get("run_settings", {})
    questions = [_dict_to_qc(q) for q in d.get("questions", [])]
    return RunConfig(
        url=d.get("url", ""),
        form_title=d.get("form_title", "Google Form"),
        pool_file=d.get("pool_file"),
        questions=questions,
        count=rs.get("count", 1),
        browser=rs.get("browser", "auto"),
        headless=rs.get("headless", False),
        min_delay=rs.get("min_delay", 0.25),
        max_delay=rs.get("max_delay", 0.75),
        page_delay_min=rs.get("page_delay_min", 0.8),
        page_delay_max=rs.get("page_delay_max", 1.4),
        max_runs=rs.get("max_runs", 500),
        log_file=rs.get("log_file", "gformbot_log.csv"),
    )


def _dict_to_qc(d: dict) -> QuestionConfig:
    return QuestionConfig(
        index=d.get("index", 0),
        text=d.get("text", ""),
        qtype=d.get("type", "unknown"),
        required=d.get("required", False),
        options=d.get("options", []),
        page=d.get("page", 1),
        fixed_value=d.get("fixed_value"),
        weights=d.get("weights"),
        max_select=d.get("max_select"),
        pool_column=d.get("pool_column"),
        text_pool=d.get("text_pool"),
    )
