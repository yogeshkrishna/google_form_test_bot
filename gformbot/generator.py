"""
Response generator.

Builds one complete answer dict {question_index: value} for a single form submission.

Priority for each question:
  1. fixed_value from config → always use this
  2. CSV pool row → matched by pool_column or auto-matched by question text
  3. Weighted random from options list
  4. text_pool (for text/textarea questions)
  5. Empty string (safe skip)
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any, Optional

from gformbot.config import QuestionConfig, RunConfig


# ---------------------------------------------------------------------------
# Pool loader
# ---------------------------------------------------------------------------

_pool_cache: dict[str, list[dict]] = {}


def load_pool(path: str) -> list[dict]:
    if path in _pool_cache:
        return _pool_cache[path]
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Pool file not found: {path}")
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"Pool file is empty: {path}")
    _pool_cache[path] = rows
    return rows


def clear_pool_cache():
    _pool_cache.clear()


# ---------------------------------------------------------------------------
# Column auto-mapper
# ---------------------------------------------------------------------------

def _find_column(row: dict, q: QuestionConfig) -> Optional[str]:
    """Return the CSV column name best matching this question."""
    if q.pool_column and q.pool_column in row:
        return q.pool_column

    # Exact match on question text
    if q.text in row:
        return q.text

    # Prefix match (question text starts the column name)
    for col in row:
        if col.startswith(q.text[:30]):
            return col

    return None


# ---------------------------------------------------------------------------
# Answer generator
# ---------------------------------------------------------------------------

def make_answer(config: RunConfig, pool_row: Optional[dict] = None) -> dict[int, Any]:
    """
    Build one answer dict.

    Parameters
    ----------
    config   : RunConfig
    pool_row : A single row from the CSV pool (or None for fully synthetic)
    """
    answer: dict[int, Any] = {}

    for q in config.questions:
        answer[q.index] = _answer_for_question(q, pool_row)

    return answer


def _answer_for_question(q: QuestionConfig, pool_row: Optional[dict]) -> Any:
    # 1. Fixed value override
    if q.fixed_value is not None:
        return q.fixed_value

    # 2. Try CSV pool
    if pool_row is not None:
        col = _find_column(pool_row, q)
        if col:
            raw = pool_row[col]
            return _coerce(q, raw)

    # 3. Random from options
    if q.options:
        return _random_from_options(q)

    # 4. Text pool
    if q.text_pool:
        return random.choice(q.text_pool)

    # 5. Empty
    return "" if q.qtype in ("text", "textarea") else None


def _coerce(q: QuestionConfig, raw: str) -> Any:
    """Convert a raw CSV string to the right Python type for this question."""
    raw = (raw or "").strip()
    if not raw:
        # Fall back to random
        if q.options:
            return _random_from_options(q)
        if q.text_pool:
            return random.choice(q.text_pool)
        return ""

    if q.qtype == "checkbox":
        # CSV stores multi-select as comma-separated values
        candidates = [v.strip() for v in raw.split(",") if v.strip()]
        # Filter to valid options only
        valid = [c for c in candidates if c in q.options] if q.options else candidates
        # Enforce exclusive options and max_select
        valid = _sanitize_checkboxes(q, valid)
        return valid

    return raw


def _sanitize_checkboxes(q: QuestionConfig, selected: list[str]) -> list[str]:
    if not selected or not q.options:
        return selected

    # Last option is often "exclusive" (e.g. "None / Not sure")
    exclusive = q.options[-1] if q.options else None
    if exclusive and exclusive in selected:
        return [exclusive]

    if q.max_select:
        selected = selected[: q.max_select]

    return selected


def _random_from_options(q: QuestionConfig) -> Any:
    if not q.options:
        return ""

    if q.qtype == "checkbox":
        max_k = q.max_select or max(1, len(q.options) - 1)
        k = random.randint(1, min(max_k, len(q.options)))
        if q.weights:
            chosen = random.choices(q.options, weights=q.weights, k=k)
        else:
            chosen = random.sample(q.options, k)
        return _sanitize_checkboxes(q, chosen)

    # radio / dropdown
    if q.weights:
        return random.choices(q.options, weights=q.weights, k=1)[0]
    return random.choice(q.options)


# ---------------------------------------------------------------------------
# Batch answer generator
# ---------------------------------------------------------------------------

def answer_stream(config: RunConfig):
    """
    Yield one answer dict per submission.
    Handles pool cycling vs. purely synthetic generation.
    """
    pool = None
    if config.pool_file:
        pool = load_pool(config.pool_file)

    while True:
        row = random.choice(pool) if pool else None
        yield make_answer(config, row)
