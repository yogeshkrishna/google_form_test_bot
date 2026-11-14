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


def _content_digest(payload):
    return hashlib.sha256(
        json.dumps(
            {k: payload.get(k) for k in ("form_fingerprint", "responses")},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


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
                "CREATE TABLE IF NOT EXISTS followups (batch TEXT NOT NULL, response INTEGER NOT NULL, "
                "section INTEGER NOT NULL, stage TEXT NOT NULL, content TEXT NOT NULL, "
                "log_path TEXT NOT NULL, title TEXT NOT NULL, "
                "PRIMARY KEY(batch,response,section,stage,content))"
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

    def record_followups(self, response, section, stage, blocks, log_path, title):
        from .config import clean_path

        path = str(clean_path(log_path).resolve())
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO followups VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        self.identity,
                        response,
                        section,
                        stage,
                        json.dumps(block, sort_keys=True, ensure_ascii=False),
                        path,
                        title,
                    )
                    for block in blocks
                ],
            )

    def followups(self, *, log_path=None, last=20):
        from .config import clean_path

        if not self.path.exists():
            return []
        where, args = (
            ("batch=?", [self.identity])
            if log_path is None
            else ("log_path=?", [str(clean_path(log_path).resolve())])
        )
        with self._connect() as connection:
            rows = connection.execute(
                "WITH scoped AS (SELECT rowid AS ordinal,* FROM followups WHERE "
                + where
                + "), recent AS (SELECT batch,response,MAX(ordinal) AS newest FROM scoped "
                "GROUP BY batch,response ORDER BY newest DESC LIMIT ?) "
                "SELECT f.batch,f.response,f.section,f.stage,f.content,f.title FROM scoped f "
                "JOIN recent r ON f.batch=r.batch AND f.response=r.response ORDER BY f.ordinal",
                [*args, last],
            ).fetchall()
        return [
            dict(batch=b, response=r, section=s, stage=t, content=json.loads(c), title=title)
            for b, r, s, t, c, title in rows
        ]

    def set(self, index: int, state: str, *, expected_content=None, check_current=None):
        if state not in {"prepared", "reviewing", "attempted", "confirmed", "uncertain"}:
            raise ValueError("Unknown execution state.")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if expected_content is not None:
                    bound = connection.execute(
                        "SELECT content FROM batches WHERE batch=?", (self.identity,)
                    ).fetchone()
                    if bound != (expected_content,):
                        raise ValueError("The batch was edited. Reopen it before submitting.")
                previous = connection.execute(
                    "SELECT state FROM responses WHERE batch=? AND response=?", (self.identity, index)
                ).fetchone()
                if previous and previous[0] == "confirmed" and state != "confirmed":
                    raise UncertainSubmission(
                        f"Response {index} is confirmed and cannot be reset by a stale execution."
                    )
                if state in {"attempted", "reviewing"}:
                    current = previous
                    if current and current[0] != "prepared":
                        raise UncertainSubmission(
                            f"Response {index} is already {current[0]}; reconcile it before another attempt."
                        )
                if check_current is not None:
                    check_current()
                connection.execute(
                    "INSERT INTO responses(batch,response,state) VALUES(?,?,?) ON CONFLICT(batch,response) DO UPDATE SET state=excluded.state",
                    (self.identity, index, state),
                )
        except (sqlite3.Error, OSError) as exc:
            raise ValueError(f"Cannot save submission state; execution stopped: {exc}") from exc

    def bind(self, payload):
        signature = _content_digest(payload)
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

    def edit(self, payload, answers, write):
        """Serialize edits against submission claims; never reset response identities or outcomes."""
        if not answers or any(
            type(index) is not int or not 1 <= index <= len(payload["responses"]) for index in answers
        ):
            raise ValueError("Choose an existing response number.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bound = connection.execute(
                "SELECT content FROM batches WHERE batch=?", (self.identity,)
            ).fetchone()
            if bound is not None and bound != (_content_digest(payload),):
                raise ValueError("This batch changed. Reopen it before editing.")
            protected = set(payload.get("submitted_indices", []))
            protected.update(
                index
                for (index,) in connection.execute(
                    "SELECT response FROM responses WHERE batch=? AND state != 'prepared'", (self.identity,)
                )
            )
            if conflict := protected.intersection(answers):
                raise ValueError(
                    f"Response {min(conflict)} is submitted or needs checking and cannot be edited."
                )
            changed = {**payload, "responses": list(payload["responses"])}
            for index, answer in answers.items():
                changed["responses"][index - 1] = {str(key): value for key, value in answer.items()}
            changed["batch_id"] = payload.get("batch_id") or self.identity
            write(changed)
            # An interrupted commit after the atomic file write leaves a binding
            # mismatch, which stops execution rather than allowing stale answers.
            connection.execute(
                "INSERT INTO batches(batch,content) VALUES(?,?) ON CONFLICT(batch) DO UPDATE SET content=excluded.content",
                (self.identity, _content_digest(changed)),
            )


def atomic_json(path: Path, payload: dict, *, compact=False):
    """Flush before replacement so a process interruption keeps a valid document."""
    import os
    import tempfile

    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
            )
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
        self.signature = _content_digest(payload)
        self.journal = Journal(self.identity)
        self.journal.bind(payload)
        self.confirmed = set(payload.get("submitted_indices", []))
        self.outcomes = self.journal.states()
        for index in self.confirmed:
            if self.outcomes.get(index) != "confirmed":
                self.journal.set(index, "confirmed")
                self.outcomes[index] = "confirmed"
        self.confirmed.update(i for i, state in self.outcomes.items() if state == "confirmed")
        self.ids = [i for i in range(1, len(payload["responses"]) + 1) if i not in self.confirmed]

    def uncertain(self):
        return [i for i in self.ids if self.outcomes.get(i) in {"attempted", "reviewing", "uncertain"}]

    def start(self):
        """Remember an execution choice even if the first connection fails."""
        if not self.payload.get("submission_started"):
            self.payload["submission_started"] = True
            self._save()

    def reconcile(self, index: int, submitted: bool):
        self.journal.set(index, "confirmed" if submitted else "prepared")
        self.outcomes[index] = "confirmed" if submitted else "prepared"
        if submitted:
            self.confirmed.add(index)
            self.ids.remove(index)
        self._save()

    def before_submit(self, local_index: int):
        original = self.ids[local_index - 1]
        self.journal.set(
            original,
            "attempted",
            expected_content=self.signature,
            check_current=self._check_current,
        )
        self.outcomes[original] = "attempted"

    def failed(self, local_index: int, uncertain: bool):
        self._set(local_index, "uncertain" if uncertain else "prepared")

    def _set(self, local_index: int, state: str):
        original = self.ids[local_index - 1]
        self.journal.set(original, state)
        self.outcomes[original] = state

    def confirm(self, local_index: int):
        original = self.ids[local_index - 1]
        # The journal must survive a subsequent CSV log or plan-file write failure.
        self.journal.set(original, "confirmed")
        self.outcomes[original] = "confirmed"
        self.confirmed.add(original)
        self._save()

    def _save(self):
        from .locking import file_lock

        with file_lock(self.path, timeout=5):
            self._check_current()
            self.payload["batch_id"] = self.identity
            self.payload["submitted_indices"] = sorted(self.confirmed)
            atomic_json(self.path, self.payload)
            self.digest = self._digest()

    def _digest(self):
        from .artifacts import digest

        return digest(self.path)

    def _check_current(self):

        if self._digest() != self.digest:
            raise ValueError(
                "The saved preview changed during execution. Check the submission journal before retrying."
            )
