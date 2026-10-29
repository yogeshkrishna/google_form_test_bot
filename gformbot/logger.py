"""Append complete response outcomes to a CSV log."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locking import file_lock

_HEADER = ["timestamp", "run", "status", "message", "answers"]
_IDENTIFIED_HEADER = _HEADER + ["batch_id", "form_url"]


def _log_header(p: Path) -> list[str] | None:
    """Read and validate under the caller's log lock."""
    if not p.exists():
        return None
    if not p.is_file():
        raise ValueError(f"Log path is not a file: {p}")
    if p.stat().st_size:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header not in (_HEADER, _IDENTIFIED_HEADER):
            raise ValueError(
                f"Log {p} uses a different format. Choose another log_file "
                "in your config; the existing log was left unchanged."
            )
        return header
    return None


def validate_log_path(log_path: str | Path) -> None:
    """Reject incompatible existing logs without racing a writer's header."""
    p = Path(log_path)
    if not p.exists():
        return
    if not p.is_file():
        raise ValueError(f"Log path is not a file: {p}")
    with file_lock(p, timeout=5):
        _log_header(p)


def log_result(
    log_path: str | Path,
    run_index: int,
    answer: dict[int, Any],
    success: bool,
    message: str,
    *,
    batch_id: str = "",
    form_url: str = "",
) -> None:
    """Write one result, preserving every answer and any existing log."""
    p = Path(log_path)
    serialized = json.dumps(answer, ensure_ascii=False, allow_nan=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p, timeout=5):
        header = _log_header(p)
        write_header = header is None
        header = header or (_IDENTIFIED_HEADER if batch_id else _HEADER)
        if batch_id and header == _HEADER:
            message = f"[batch={batch_id}; form={form_url}] {message}"

        with p.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            row = [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                run_index,
                "OK" if success else "ERROR",
                message,
                serialized,
            ]
            if header == _IDENTIFIED_HEADER:
                row.extend([batch_id, form_url])
            writer.writerow(row)
