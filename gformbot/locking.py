"""Small process locks; the OS releases ownership after interruption or exit."""

import os
import time
from contextlib import contextmanager
from functools import partial


class FileLockError(OSError):
    pass


@contextmanager
def file_lock(path, *, timeout=0):
    # Keep the sidecar: unlinking a lock while another process waits can create
    # two unrelated locks for the same target. No answer data is written here.
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        # Windows can lock beyond EOF; initializing byte zero races its owner.
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            acquire = partial(msvcrt.locking, handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = partial(msvcrt.locking, handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            acquire = partial(fcntl.flock, handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = partial(fcntl.flock, handle, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise FileLockError(f"Cannot lock {path}; another process may be using it.") from exc
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            release()
