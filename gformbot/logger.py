"""Append complete response outcomes to a CSV log."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_HEADER = ["timestamp", "run", "status", "message", "answers"]


def validate_log_path(log_path: str | Path) -> None:
    """Reject incompatible existing logs before a browser run starts."""
    p = Path(log_path)
    if not p.exists():
        return
    if not p.is_file():
        raise ValueError(f"Log path is not a file: {p}")
    if p.stat().st_size:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header != _HEADER:
            raise ValueError(
                f"Log {p} uses a different format. Choose another log_file "
                "in your config; the existing log was left unchanged."
            )


def log_result(
    log_path: str | Path,
    run_index: int,
    answer: dict[int, Any],
    success: bool,
    message: str,
) -> None:
    """Write one result, preserving every answer and any existing log."""
    p = Path(log_path)
    serialized = json.dumps(answer, ensure_ascii=False, allow_nan=False)
    validate_log_path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_header = not p.exists() or p.stat().st_size == 0

    with p.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(_HEADER)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            run_index,
            "OK" if success else "ERROR",
            message,
            serialized,
        ])
