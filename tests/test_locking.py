import os
import subprocess
import sys
from pathlib import Path

import pytest

from gformbot.locking import FileLockError, file_lock


@pytest.mark.parametrize("contents", [b"", b"\0"], ids=["empty", "legacy-byte"])
def test_contended_sidecar_times_out_without_rewriting_it(tmp_path, contents):
    target = tmp_path / "shared.yaml"
    sidecar = target.with_name(target.name + ".lock")
    sidecar.write_bytes(contents)
    with sidecar.open("a+b") as owner:
        owner.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(owner, fcntl.LOCK_UN)

        try:
            with pytest.raises(FileLockError, match="Cannot lock"):
                with file_lock(target, timeout=0.05):
                    pytest.fail("An independent owner holds the lock")
        finally:
            unlock()
    assert sidecar.read_bytes() == contents
    with file_lock(target):
        pass
    assert sidecar.read_bytes() == contents


def test_sidecar_open_permission_error_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def denied_open(*args, **kwargs):
        calls.append(args)
        raise PermissionError("Sidecar creation denied")

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(PermissionError, match="Sidecar creation denied"):
        with file_lock(tmp_path / "private.yaml", timeout=5):
            pytest.fail("Permission failure must stop before lock acquisition")
    assert len(calls) == 1


def test_process_exclusion_and_release_after_exception(tmp_path):
    target = tmp_path / "shared.yaml"
    script = """
import sys
from pathlib import Path
from gformbot.locking import FileLockError, file_lock
try:
    with file_lock(Path(sys.argv[1]), timeout=0.05):
        print("acquired")
except FileLockError:
    print("busy")
"""

    def another_process():
        return subprocess.run(
            [sys.executable, "-c", script, str(target)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    with pytest.raises(RuntimeError, match="Interrupted owner"):
        with file_lock(target):
            assert another_process() == "busy"
            raise RuntimeError("Interrupted owner")
    assert another_process() == "acquired"
    assert target.with_name(target.name + ".lock").exists()
