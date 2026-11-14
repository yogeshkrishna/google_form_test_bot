"""Local, non-executable checkpoints for unfinished AI generation."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import previews
from .config import clean_path, config_from_dict, config_to_dict, validate_config
from .inspector import clean_url, schema_fingerprint
from .locking import file_lock
from .state import atomic_json

_KIND = "gformbot-ai-draft"
_VERSION = 2
_MAX_BYTES = 32 * 1024 * 1024
_FIELDS = {
    "kind",
    "version",
    "identity",
    "created_at",
    "owner_path",
    "config",
    "form_fingerprint",
    "reference",
    "instructions",
    "partials",
    "state",
    "checksum",
}


def _encode(value, *, sort_keys=True):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("AI draft contains invalid JSON data.") from exc
    if len(raw) > _MAX_BYTES:
        raise ValueError("AI draft exceeds the 32 MiB size limit.")
    return raw


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _fingerprint(config):
    normalized = replace(config, url=clean_url(config.url, allow_short=False))
    return _digest({"schema": schema_fingerprint(normalized), "instructions": config.instructions})


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("AI draft contains duplicate JSON keys.")
        result[key] = value
    return result


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Choose an existing AI draft file, not a link or directory.")
    with path.open("rb") as handle:
        raw = handle.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("AI draft exceeds the 32 MiB size limit.")
    try:
        payload = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Cannot read AI draft JSON.") from exc
    return payload, hashlib.sha256(raw).hexdigest()


def _answers(value, known):
    if not isinstance(value, dict) or any(key not in known for key in value):
        raise ValueError("AI draft answers contain unknown question IDs.")
    for answer in value.values():
        if isinstance(answer, str):
            continue
        if isinstance(answer, list) and all(isinstance(item, str) for item in answer):
            continue
        if (
            isinstance(answer, dict)
            and set(answer) == {"choices", "other"}
            and isinstance(answer["other"], str)
            and isinstance(answer["choices"], list)
            and all(isinstance(item, str) for item in answer["choices"])
        ):
            continue
        raise ValueError("AI draft contains an invalid answer value.")


def _strategy(state):
    return state.get("strategy", "partition") if isinstance(state, dict) else "partition"


def _validate_review(review, count, known):
    if not isinstance(review, dict) or set(review) != {"checked", "issues"}:
        raise ValueError("AI draft review progress is invalid.")

    def response_id(value):
        return type(value) is int and 1 <= value <= count

    checked, issues = review["checked"], review["issues"]
    if (
        not isinstance(checked, list)
        or not all(response_id(value) for value in checked)
        or len(set(checked)) != len(checked)
    ):
        raise ValueError("AI draft reviewed response IDs are invalid.")
    if not isinstance(issues, list):
        raise ValueError("AI draft review issues are invalid.")
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or set(issue) - {"blocking"} != {"response", "question", "reason"}
            or not response_id(issue["response"])
            or (
                issue["question"] is not None
                and (type(issue["question"]) is not int or str(issue["question"]) not in known)
            )
            or not isinstance(issue["reason"], str)
            or not issue["reason"].strip()
            or type(issue.get("blocking", True)) is not bool
        ):
            raise ValueError("AI draft review issue is invalid.")


def _validate_state(state, version, config, known):
    fields = {"profiles"}
    if version == _VERSION:
        fields |= {"strategy", "review", "setup"}
    if not isinstance(state, dict) or set(state) != fields:
        raise ValueError("AI draft progress is invalid.")
    strategy = _strategy(state)
    if strategy not in ("flat", "partition"):
        raise ValueError("AI draft generation strategy is invalid.")
    if version == _VERSION:
        if not isinstance(state["setup"], dict):
            raise ValueError("AI draft generation setup is invalid.")
        _validate_review(state["review"], config.count, known)
    profiles = state["profiles"]
    if not isinstance(profiles, list) or len(profiles) != config.count:
        raise ValueError("AI draft progress count does not match the request.")
    pages = max([1, len(config.sections), *(q.page for q in config.questions)])
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {"answers", "position", "route"}:
            raise ValueError("AI draft profile progress is invalid.")
        answers, position, route = profile["answers"], profile["position"], profile["route"]
        _answers(answers, known)
        if type(position) is not int or not 0 <= position <= len(config.questions):
            raise ValueError("AI draft progress position is invalid.")
        if strategy == "flat":
            if (
                route is not None
                or position not in (0, 1)
                or (set(answers) != (known if position else set()))
            ):
                raise ValueError("AI draft flat profile progress is invalid.")
        elif route is None:
            if position != 0:
                raise ValueError("AI draft progress has no selected route.")
        elif (
            not isinstance(route, list)
            or len(route) > pages
            or any(type(page) is not int or not 1 <= page <= pages for page in route)
        ):
            raise ValueError("AI draft route is invalid.")
    if strategy == "flat" and any(
        profiles[number - 1]["position"] != 1 for number in state["review"]["checked"]
    ):
        raise ValueError("AI draft review includes a pending response.")


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) not in (_FIELDS, _FIELDS | {"options"}):
        raise ValueError("Choose a GFormBot AI draft, rather than a response plan.")
    version = payload["version"]
    if (
        payload["kind"] != _KIND
        or type(version) is not int
        or version not in (1, _VERSION)
        or ("options" in payload) != (version == _VERSION)
    ):
        raise ValueError("Unsupported AI draft format.")
    if payload["checksum"] != _digest({k: v for k, v in payload.items() if k != "checksum"}):
        raise ValueError("AI draft content changed or is damaged.")
    try:
        if str(uuid.UUID(payload["identity"])) != payload["identity"]:
            raise ValueError
        datetime.fromisoformat(payload["created_at"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("AI draft identity or timestamp is invalid.") from exc
    if not isinstance(payload["owner_path"], str) or not Path(payload["owner_path"]).is_absolute():
        raise ValueError("AI draft ownership is invalid.")
    config = config_from_dict(payload["config"])
    if payload["form_fingerprint"] != _fingerprint(config):
        raise ValueError("AI draft form metadata changed.")
    if not isinstance(payload["instructions"], str):
        raise ValueError("AI draft instructions must be text.")
    options = payload.get("options", {})
    if not isinstance(options, dict) or options.get("browser") not in (
        None,
        "auto",
        "chrome",
        "firefox",
        "edge",
    ):
        raise ValueError("AI draft generation options are invalid.")
    known = {str(q.index) for q in config.questions}
    for name in ("reference", "partials"):
        rows = payload[name]
        if rows is not None:
            if not isinstance(rows, list) or len(rows) > 10_000:
                raise ValueError(f"AI draft {name} must contain at most 10000 response rows.")
            if name == "partials" and len(rows) != config.count:
                raise ValueError("AI draft partial response count does not match the request.")
            for row in rows:
                _answers(row, known)
    _validate_state(payload["state"], version, config, known)
    return config


def _rows(rows):
    return None if rows is None else [{int(key): value for key, value in row.items()} for row in rows]


def saved_paths():
    return (previews.index_path().parent / "ai-drafts").glob("*.json")


def partition_flat_profiles(config, profiles, partials):
    """Keep accepted flat profiles and start only pending profiles in the partitioner."""
    from .ai_partition import _units
    from .generator import validate_answer
    from .routing import Routing

    known = {str(q.index) for q in config.questions}
    _validate_state(
        {"strategy": "flat", "profiles": profiles, "review": {"checked": [], "issues": []}, "setup": {}},
        _VERSION,
        config,
        known,
    )
    routes, units = Routing(config), _units(config)
    fixed = {q.index: q.fixed_value for q in config.questions if q.fixed_value is not None}
    presets = partials if partials is not None else [{}] * config.count
    converted = []
    for profile, preset in zip(profiles, presets, strict=True):
        base = validate_answer(config, preset, routing=routes, partial=True) if preset else {}
        base.update(fixed)
        if not profile["position"]:
            converted.append(
                {"answers": {str(key): value for key, value in base.items()}, "position": 0, "route": None}
            )
            continue
        answers = {int(key): value for key, value in profile["answers"].items()}
        if validate_answer(config, answers, routing=routes) != answers:
            raise ValueError("AI draft contains answers that require normalization.")
        if any(answers[key] != value for key, value in base.items()):
            raise ValueError("AI draft changed a preselected answer.")
        route = list(routes.walk(answers))
        converted.append(
            {
                "answers": deepcopy(profile["answers"]),
                "position": sum(len(units[page]) for page in route),
                "route": route,
            }
        )
    return converted


class Draft:
    """An owned snapshot; a copied draft retains the same submission identity."""

    def __init__(self, path, payload, digest, config):
        self.path, self._payload, self._digest = path, payload, digest
        self.config = config
        self.identity = payload["identity"]
        self.reference = _rows(payload["reference"])
        self.instructions = payload["instructions"]
        self.partials = _rows(payload["partials"])
        self.state = payload["state"]
        self._strategy = _strategy(self.state)

    @property
    def options(self):
        """Return immutable job options without exposing the saved copy for mutation."""
        return deepcopy(self._payload.get("options", {}))

    @property
    def strategy(self):
        return self._strategy

    @property
    def created_at(self):
        return datetime.fromisoformat(self._payload["created_at"])

    def progress_label(self):
        profiles = self.state["profiles"]
        total = len(profiles)
        if self.strategy == "flat":
            generated = sum(profile["position"] == 1 for profile in profiles)
        else:
            from .ai_partition import _units

            sizes = {page: len(units) for page, units in _units(self.config).items()}
            generated = routed = saved_groups = total_groups = 0
            for profile in profiles:
                if profile["route"] is None:
                    continue
                groups = sum(sizes.get(page, 0) for page in profile["route"])
                routed += 1
                saved_groups += profile["position"]
                total_groups += groups
                generated += profile["position"] == groups
            if generated < total and saved_groups:
                if routed == total:
                    progress = f"{saved_groups}/{total_groups} question groups saved"
                else:
                    unit = "question group" if saved_groups == 1 else "question groups"
                    progress = f"{saved_groups} {unit} saved"
                return f"{generated}/{total} generated · {progress}"
        if generated == total:
            checked = len(self.state.get("review", {}).get("checked", ()))
            review_total = self.state.get("setup", {}).get("review_total")
            if type(review_total) is int and 0 < review_total <= total and checked <= review_total:
                return f"{checked}/{review_total} quality checked"
            if checked:
                return f"{checked} quality checked"
        return f"{generated}/{total} generated"

    @classmethod
    def create(cls, config, reference, instructions, partials, state, *, options=None):
        validate_config(config)
        identity = str(uuid.uuid4())
        directory = previews.index_path().parent / "ai-drafts"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = (directory / f"{identity}.json").absolute()
        modern = options is not None or isinstance(state, dict) and "strategy" in state
        payload = {
            "kind": _KIND,
            "version": _VERSION if modern else 1,
            "identity": identity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "owner_path": str(path),
            "config": config_to_dict(config),
            "form_fingerprint": _fingerprint(config),
            "reference": reference,
            "instructions": instructions,
            "partials": partials,
            "state": state,
        }
        if modern:
            payload["options"] = {} if options is None else options
        # Freeze caller-owned maps and normalize integer answer keys once.
        payload = json.loads(_encode(payload, sort_keys=False), object_pairs_hook=_pairs)
        payload["checksum"] = _digest(payload)
        stored = _validate(payload)
        # The actual compact JSON is shorter than this canonical encoding,
        # even with the trailing newline written by atomic_json.
        _encode(payload)
        with file_lock(path):
            if path.exists() or path.is_symlink():
                raise ValueError("AI draft destination already exists.")
            atomic_json(path, payload, compact=True)
        return cls(path, payload, _read(path)[1], stored)

    @classmethod
    def read(cls, path):
        path = clean_path(path).absolute()
        payload, digest = _read(path)
        config = _validate(payload)
        return cls(path, payload, digest, config)

    @classmethod
    def load(cls, path, current):
        draft = cls.read(path)
        draft.check_form(current)
        return draft

    def check_form(self, current):
        if self._payload["form_fingerprint"] != _fingerprint(current):
            raise ValueError("The form or its instructions changed. Start a new generation request.")

    def _unchanged(self):
        payload, digest = _read(self.path)
        if digest != self._digest:
            raise ValueError("AI draft changed in another process. Reopen it before continuing.")
        return payload

    def save(self, state):
        with file_lock(self.path):
            original = self._unchanged()
            strategy = _strategy(state)
            if strategy != self.strategy and not (
                self.strategy == "flat"
                and isinstance(state, dict)
                and state.get("strategy") == "partition"
                and state.get("profiles")
                == partition_flat_profiles(
                    config_from_dict(original["config"]), original["state"]["profiles"], original["partials"]
                )
            ):
                raise ValueError("AI draft generation strategy changed. Start a new generation request.")
            payload = {**original, "state": state}
            if isinstance(state, dict) and "strategy" in state and payload["version"] == 1:
                payload.update(version=_VERSION, options={})
            payload.pop("checksum")
            payload = json.loads(_encode(payload, sort_keys=False), object_pairs_hook=_pairs)
            payload["checksum"] = _digest(payload)
            _validate(payload)
            _encode(payload)
            atomic_json(self.path, payload, compact=True)
            self._digest = _read(self.path)[1]
        self._payload, self.state, self._strategy = payload, payload["state"], strategy

    def finish(self):
        """Remove only our original unchanged snapshot after its plan is saved."""
        with file_lock(self.path):
            self._unchanged()
            if os.path.normcase(str(self.path)) == os.path.normcase(self._payload["owner_path"]):
                self.path.unlink()
