"""Append complete response outcomes to a CSV log."""

from __future__ import annotations

import csv
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locking import file_lock

_HEADER = ["timestamp", "run", "status", "message", "answers"]
_IDENTIFIED_HEADER = _HEADER + ["batch_id", "form_url"]
_TITLED_HEADER = _IDENTIFIED_HEADER + ["form_title"]
_LEGACY_ID = re.compile(r"^\[batch=([^;]+); form=([^\]]*)\]\s*")


def _log_header(p: Path) -> list[str] | None:
    """Read and validate under the caller's log lock."""
    if not p.exists():
        return None
    if not p.is_file():
        raise ValueError(f"Log path is not a file: {p}")
    if p.stat().st_size:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header not in (_HEADER, _IDENTIFIED_HEADER, _TITLED_HEADER):
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
    form_title: str = "",
    uncertain: bool = False,
) -> None:
    """Write one result, preserving every answer and any existing log."""
    p = Path(log_path)
    serialized = json.dumps(answer, ensure_ascii=False, allow_nan=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p, timeout=5):
        header = _log_header(p)
        write_header = header is None
        header = header or (_TITLED_HEADER if form_title else _IDENTIFIED_HEADER if batch_id else _HEADER)
        if batch_id and header == _HEADER:
            message = f"[batch={batch_id}; form={form_url}] {message}"

        with p.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            row = [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                run_index,
                "UNCERTAIN" if uncertain else "OK" if success else "ERROR",
                message,
                serialized,
            ]
            if header != _HEADER:
                row.extend([batch_id, form_url])
            if header == _TITLED_HEADER:
                row.append(form_title)
            writer.writerow(row)


def recent_results(log_path: Path, last: int):
    """Read newest first across the supported on-disk log formats."""
    with log_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return [], 0
        if reader.fieldnames not in (_HEADER, _IDENTIFIED_HEADER, _TITLED_HEADER):
            raise ValueError(f"Unrecognized run log format: {log_path}")
        recent, skipped = deque(maxlen=last), 0
        for row in reader:
            if None in row or None in row.values():
                skipped += 1
                continue
            if match := _LEGACY_ID.match(row["message"]):
                row.setdefault("batch_id", match[1])
                row.setdefault("form_url", match[2])
                row["message"] = row["message"][match.end() :]
            recent.append(row)
    return list(reversed(recent)), skipped


def result_label(row):
    """Display a short form label when an older row has only the URL."""
    if row.get("form_title"):
        return row["form_title"]
    parts = row.get("form_url", "").split("/")
    if parts[-1] == "viewform" and len(parts) >= 2:
        return "Form " + parts[-2][-8:]
    return "Unknown form"


def result_time(row):
    try:
        return datetime.fromisoformat(row["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return row["timestamp"]
