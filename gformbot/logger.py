"""
Logger — writes a CSV run log after each submission attempt.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any


def log_result(
    log_path: str | Path,
    run_index: int,
    answer: dict[int, Any],
    success: bool,
    message: str,
) -> None:
    p = Path(log_path)
    write_header = not p.exists()

    with p.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "run", "status", "message", "q1", "q2"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            run_index,
            "OK" if success else "ERROR",
            message,
            answer.get(1, ""),
            answer.get(2, ""),
        ])
