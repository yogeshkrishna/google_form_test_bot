"""Remember preview locations; the saved plan remains the source of truth."""

import json
import os
from pathlib import Path


def index_path():
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "gformbot" / "previews.json"


def _remembered():
    try:
        values = json.loads(index_path().read_text(encoding="utf-8"))
        return [Path(p) for p in values if isinstance(p, str)] if isinstance(values, list) else []
    except (OSError, ValueError):
        return []


def remember(path):
    from .locking import file_lock
    from .state import atomic_json

    target = index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target, timeout=5):
        paths = {str(p.resolve()) for p in _remembered() if p.is_file()}
        paths.add(str(Path(path).resolve()))
        atomic_json(target, sorted(paths))


def available():
    """List existing candidates without reading unrelated directories recursively."""
    paths = {p.resolve() for p in _remembered() if p.is_file() and not p.is_symlink()}
    paths.update(p.resolve() for p in Path.cwd().glob("*.json") if p.is_file() and not p.is_symlink())
    return sorted(paths)
