"""Opt-in, local timing for one AI generation run."""

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from uuid import uuid4

_REQUEST = ContextVar("generation_timing_request", default=None)
_PHASE = ContextVar("generation_timing_phase", default=None)


@dataclass
class _Phase:
    name: str
    nested_seconds: float = 0.0


def _ms(seconds):
    return round(seconds * 1000, 2)


def time_connections(pool):
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPSConnectionPool

    class TimedConnection(HTTPSConnection):
        def connect(self):
            request = _REQUEST.get()
            if request is None:
                return super().connect()
            started = monotonic()
            try:
                return super().connect()
            finally:
                request["connection_attempts"] += 1
                request["connection_ms"] += _ms(monotonic() - started)

    class TimedPool(HTTPSConnectionPool):
        ConnectionCls = TimedConnection

    pool.pool_classes_by_scheme = {**pool.pool_classes_by_scheme, "https": TimedPool}


class GenerationTiming:
    def __init__(self):
        self.started = monotonic()
        self._lock = Lock()
        self.requests = []
        self.phases = defaultdict(float)
        self.waits = defaultdict(float)

    @contextmanager
    def phase(self, name):
        parent = _PHASE.get()
        phase = _Phase(name)
        started = monotonic()
        token = _PHASE.set(phase)
        try:
            yield
        finally:
            elapsed = monotonic() - started
            _PHASE.reset(token)
            # A repair runs inside review. Count its time only under repair.
            if parent is not None:
                parent.nested_seconds += elapsed
            with self._lock:
                self.phases[name] += elapsed - phase.nested_seconds

    @contextmanager
    def request(self, stage):
        phase = _PHASE.get()
        request = {
            "phase": phase.name if phase is not None else "generate",
            "stage": stage,
            "connection_attempts": 0,
            "connection_ms": 0,
        }
        token = _REQUEST.set(request)
        started = monotonic()
        try:
            yield request
        except BaseException:
            request["failed"] = True
            raise
        finally:
            _REQUEST.reset(token)
            request["total_ms"] = _ms(monotonic() - started)
            with self._lock:
                self.requests.append(request)

    def wait(self, kind, seconds):
        with self._lock:
            self.waits[kind] += seconds

    def save(self, *, completed):
        from .previews import index_path
        from .state import atomic_json

        try:
            path = index_path().with_name("ai-timing") / f"{uuid4().hex}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(
                path,
                {
                    "completed": completed,
                    "wall_ms": _ms(monotonic() - self.started),
                    "phase_ms": {name: _ms(value) for name, value in self.phases.items()},
                    "worker_wait_ms": {name: _ms(value) for name, value in self.waits.items()},
                    "requests": self.requests,
                },
            )
            return path
        except OSError:
            return None
