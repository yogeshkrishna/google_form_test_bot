"""Durable submission outcomes, shared by copied plans and interrupted runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import previews


class UncertainSubmission(ValueError):
    """A previous attempt needs a human decision before it can be repeated."""


def batch_id(payload: dict) -> str:
    explicit = payload.get("batch_id")
    if explicit is not None:
        try:
            return str(uuid.UUID(explicit))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("The saved batch ID is invalid.") from exc
    # Legacy plans get a deterministic identity, including when the file is copied.
    immutable = {k: payload.get(k) for k in ("created_at", "form_fingerprint", "responses")}
    digest = hashlib.sha256(json.dumps(immutable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "gformbot:" + digest))


class Journal:
    def __init__(self, identity: str):
        self.identity = identity
        self.path = previews.index_path().with_name("runs.sqlite3")

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS responses (batch TEXT NOT NULL, response INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(batch, response))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS batches (batch TEXT PRIMARY KEY, content TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_claims (batch TEXT NOT NULL, response INTEGER NOT NULL, token TEXT NOT NULL, PRIMARY KEY(batch,response))"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def states(self) -> dict[int, str]:
        if not self.path.exists():
            return {}
        try:
            with self._connect() as connection:
                return dict(
                    connection.execute(
                        "SELECT response, state FROM responses WHERE batch=?", (self.identity,)
                    )
                )
        except (sqlite3.Error, OSError) as exc:
            raise ValueError(f"Cannot read submission journal {self.path}: {exc}") from exc

    def set(self, index: int, state: str, *, from_review=None):
        if state not in {"prepared", "reviewing", "attempted", "confirmed", "uncertain"}:
            raise ValueError("Unknown execution state.")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT state FROM responses WHERE batch=? AND response=?", (self.identity, index)
                ).fetchone()
                if previous and previous[0] == "confirmed" and state != "confirmed":
                    raise UncertainSubmission(
                        f"Response {index} is confirmed and cannot be reset by a stale execution."
                    )
                if from_review is not None:
                    claim = connection.execute(
                        "SELECT token FROM review_claims WHERE batch=? AND response=?", (self.identity, index)
                    ).fetchone()
                    if state != "attempted" or previous != ("reviewing",) or claim != (from_review,):
                        raise UncertainSubmission(
                            f"Response {index}'s review changed; reopen and reconcile it before submitting."
                        )
                if state in {"attempted", "reviewing"}:
                    current = previous
                    own_review = state == "attempted" and from_review is not None
                    if current and current[0] != "prepared" and not own_review:
                        raise UncertainSubmission(
                            f"Response {index} is already {current[0]}; reconcile it before another attempt."
                        )
                connection.execute(
                    "INSERT INTO responses(batch,response,state) VALUES(?,?,?) ON CONFLICT(batch,response) DO UPDATE SET state=excluded.state",
                    (self.identity, index, state),
                )
                connection.execute(
                    "DELETE FROM review_claims WHERE batch=? AND response=?", (self.identity, index)
                )
                token = uuid.uuid4().hex if state == "reviewing" else None
                if token is not None:
                    connection.execute(
                        "INSERT INTO review_claims VALUES(?,?,?)", (self.identity, index, token)
                    )
                return token
        except (sqlite3.Error, OSError) as exc:
            raise ValueError(f"Cannot save submission state; execution stopped: {exc}") from exc

    def bind(self, payload):
        signature = hashlib.sha256(
            json.dumps(
                {k: payload.get(k) for k in ("form_fingerprint", "responses")},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT content FROM batches WHERE batch=?", (self.identity,)
            ).fetchone()
            if previous and previous[0] != signature:
                raise ValueError(
                    "This batch's prepared answers were edited after execution began. Save them as a new batch before executing."
                )
            connection.execute(
                "INSERT OR IGNORE INTO batches(batch,content) VALUES(?,?)", (self.identity, signature)
            )


def atomic_json(path: Path, payload: dict):
    """Flush before replacement so a process interruption keeps a valid document."""
    import os
    import tempfile

    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class Session:
    """Track original response IDs independently of display logs and plan copies."""

    def __init__(self, path: Path, payload: dict, expected_digest: str):
        from .artifacts import digest

        self.path = path
        self.payload = payload
        self.digest = expected_digest
        if digest(path) != expected_digest:
            raise ValueError(
                "The saved preview changed while you were reviewing it. Reopen it before running."
            )
        self.identity = batch_id(payload)
        self.journal = Journal(self.identity)
        self.journal.bind(payload)
        self.confirmed = set(payload.get("submitted_indices", []))
        self.outcomes = self.journal.states()
        self._owned_reviews = {}
        for index in self.confirmed:
            if self.outcomes.get(index) != "confirmed":
                self.journal.set(index, "confirmed")
                self.outcomes[index] = "confirmed"
        self.confirmed.update(i for i, state in self.outcomes.items() if state == "confirmed")
        self.ids = [i for i in range(1, len(payload["responses"]) + 1) if i not in self.confirmed]

    def uncertain(self):
        return [i for i in self.ids if self.outcomes.get(i) in {"attempted", "reviewing", "uncertain"}]

    def reconcile(self, index: int, submitted: bool):
        self.journal.set(index, "confirmed" if submitted else "prepared")
        self._owned_reviews.pop(index, None)
        self.outcomes[index] = "confirmed" if submitted else "prepared"
        if submitted:
            self.confirmed.add(index)
            self.ids.remove(index)
        self._save()

    def before_submit(self, local_index: int):
        original = self.ids[local_index - 1]
        self.journal.set(original, "attempted", from_review=self._owned_reviews.get(original))
        self._owned_reviews.pop(original, None)
        self.outcomes[original] = "attempted"

    def review(self, local_index: int):
        self._owned_reviews[self.ids[local_index - 1]] = self._set(local_index, "reviewing")

    def failed(self, local_index: int, uncertain: bool):
        self._set(local_index, "uncertain" if uncertain else "prepared")

    def _set(self, local_index: int, state: str):
        original = self.ids[local_index - 1]
        token = self.journal.set(original, state)
        self.outcomes[original] = state
        if state != "reviewing":
            self._owned_reviews.pop(original, None)
        return token

    def confirm(self, local_index: int):
        original = self.ids[local_index - 1]
        # The journal must survive a subsequent CSV log or plan-file write failure.
        self.journal.set(original, "confirmed")
        self._owned_reviews.pop(original, None)
        self.outcomes[original] = "confirmed"
        self.confirmed.add(original)
        self._save()

    def _save(self):
        from .artifacts import digest

        if digest(self.path) != self.digest:
            raise ValueError(
                "The saved preview changed during execution. Check the submission journal before retrying."
            )
        self.payload["batch_id"] = self.identity
        self.payload["submitted_indices"] = sorted(self.confirmed)
        atomic_json(self.path, self.payload)
        self.digest = digest(self.path)
