"""Remove only unchanged output files owned by a completed response plan."""
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def describe(paths, directory):
    directory = Path(directory).resolve()
    result = []
    for path in paths:
        path = Path(path)
        if path.is_symlink() or path.resolve().parent != directory:
            raise ValueError("Generated CSV and response plan must share an output directory.")
        result.append({"name": path.name, "sha256": digest(path)})
    return result


def cleanup(plan, expected_digest):
    """Return removed and preserved paths; never scan or delete input data."""
    plan = Path(plan)
    removed, preserved = [], []
    try:
        if plan.is_symlink() or digest(plan) != expected_digest:
            return removed, [plan]
        payload = json.loads(plan.read_text(encoding="utf-8"))
        if payload.get("kind") != "gformbot-run" or payload.get("schema_version") != 1:
            return removed, [plan]
        if not payload.get("cleanup_on_success", True):
            return removed, preserved
        source = payload.get("config", {}).get("pool_file")
        source = Path(source).resolve() if source else None
        directory = plan.resolve().parent
        for item in payload.get("temporary_files", []):
            name = item.get("name", "") if isinstance(item, dict) else ""
            path = directory / name
            if (not name or Path(name).name != name or path.suffix.lower() != ".csv"
                    or path.is_symlink() or path.resolve().parent != directory or path.resolve() == source):
                preserved.append(path)
                continue
            if not path.exists():
                continue
            try:
                if digest(path) != item.get("sha256"):
                    preserved.append(path)
                    continue
                path.unlink()
                removed.append(path)
            except OSError:
                preserved.append(path)
        # Preserve the manifest when a changed or inaccessible CSV needs review.
        if preserved:
            preserved.append(plan)
        else:
            plan.unlink()
            removed.append(plan)
    except (OSError, ValueError, TypeError, AttributeError):
        preserved.append(plan)
    return removed, preserved
