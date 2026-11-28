"""Drive the installed gformbot launcher in a real terminal without network access."""

import codecs
import csv
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GFORMBOT_TEST_INSTALLED_WIZARD") != "1",
    reason="run with GFORMBOT_TEST_INSTALLED_WIZARD=1 against a freshly installed wheel",
)

URL = "https://docs.google.com/forms/d/e/offline-wizard-fixture/viewform"
START = r"(?m)^> Start a new form"
SOURCES = r"(?m)^> Generate answers with AI"
CSV_PROMPT = r"Paste a CSV path, or press Enter to open the file picker"
INSTRUCTIONS = "Use concise handoff notes."
UP, DOWN, ENTER = "\x1b[A", "\x1b[B", "\r"
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


class Console:
    def __init__(self, read, write):
        self.write = write
        self.pending = Queue()
        self.output = ""
        self.cursor = 0

        def reader():
            try:
                for chunk in iter(read, ""):
                    self.pending.put(chunk)
            except (EOFError, OSError):
                pass  # ConPTY gives EOFError, while Linux reports EIO at the slave's close.
            finally:
                self.pending.put(None)

        Thread(target=reader, daemon=True).start()

    def expect(self, pattern=None):
        deadline = monotonic() + 20
        while True:
            visible = ANSI.sub("", self.output)
            if pattern is not None and (match := re.search(pattern, visible[self.cursor :])):
                self.cursor += match.end()
                return
            try:
                chunk = self.pending.get(timeout=max(0, deadline - monotonic()))
            except Empty:
                pytest.fail(f"Timed out waiting for {pattern!r}. Terminal output:\n{visible[-5000:]}")
            if chunk is None:
                if pattern is None:
                    return
                pytest.fail(f"Terminal closed before {pattern!r}. Output:\n{visible[-5000:]}")
            self.output += chunk

    def answer(self, keys, next_pattern):
        self.write(keys + ENTER)
        self.expect(next_pattern)

    def exit(self):
        self.expect(START)
        self.write(DOWN * 3 + ENTER)
        self.expect()


@contextmanager
def terminal(folder, env):
    executable = shutil.which("gformbot", path=env["PATH"])
    assert executable and Path(executable).parent.resolve() == Path(sys.executable).parent.resolve()
    if os.name == "nt":
        from winpty import PtyProcess

        process = PtyProcess.spawn([executable], cwd=str(folder), env=env, dimensions=(30, 120))
        try:
            yield Console(lambda: process.read(4096), process.write)
        finally:
            process.close(force=True)
        return

    import fcntl
    import pty
    import signal
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(folder)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            os.execve(executable, [executable], env)
        finally:
            os._exit(127)
    decode = codecs.getincrementaldecoder("utf-8")("replace")

    def read():
        while data := os.read(fd, 4096):
            if chunk := decode.decode(data):
                return chunk
        return ""

    try:
        yield Console(read, lambda value: os.write(fd, value.encode("utf-8")))
    finally:
        os.close(fd)
        if os.waitpid(pid, os.WNOHANG)[0] == 0:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.fixture
def sandbox(tmp_path):
    fixtures = tmp_path / "child-fixtures"
    fixtures.mkdir()
    shutil.copyfile(
        Path(__file__).with_name("fixtures") / "wizard_sitecustomize.py", fixtures / "sitecustomize.py"
    )
    state = tmp_path / "state"
    guide = tmp_path / "example guide.csv"
    guide.write_text("Q1,Q2\nA,Example from the guide CSV.\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
        "PYTHONPATH": str(fixtures),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "XDG_STATE_HOME": str(state),
        "LOCALAPPDATA": str(state),
        "GFORMBOT_DEBUG": "1",
        "GFORMBOT_WIZARD_FIXTURE_URL": URL,
        "NO_COLOR": "1",
        "TERM": "xterm-256color",
    }
    return env, guide


def _start(console, *, quick):
    console.expect(START)
    console.answer(UP if quick else "", "Paste your public Google Form link")
    console.answer(URL, "Number of responses" if quick else SOURCES)


def _quick(console, guide=None, *, count="", invalid=False):
    _start(console, quick=True)
    if invalid:
        console.answer("501", "Enter a whole number between 1 and 500")
        console.expect("Number of responses")
    console.answer(count, "Extra instructions")
    console.answer(INSTRUCTIONS, r"Use (?:an existing|a) CSV as a guide")
    if guide is None:
        console.write(ENTER)
    else:
        console.write("y")
        console.expect(CSV_PROMPT)
        console.write(str(guide) + ENTER)


def _normal(console, guide=None, *, count=""):
    _start(console, quick=False)
    console.answer("", r"(?m)^> Create new fictional responses")
    console.answer(DOWN if guide else "", "Number of new responses")
    if guide is not None:
        console.answer(count, CSV_PROMPT)
        console.answer(str(guide), "Extra instructions")
    else:
        console.answer(count, "Extra instructions")
    console.write(INSTRUCTIONS + ENTER)


def _trace(folder, event):
    path = folder / "calls.jsonl"
    return [
        call
        for line in path.read_text(encoding="utf-8").splitlines()
        if (call := json.loads(line))["event"] == event
    ]


def _assert_rows(folder, responses):
    posts = _trace(folder, "submit")
    assert [post["choice"] for post in posts] == ["A" if n % 2 == 0 else "B" for n in responses]
    assert [post["answer"] for post in posts] == [
        f"Fixture answer {n} records a distinct handoff." for n in responses
    ]
    with (folder / "gformbot-runs.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["status"] for row in rows] == ["OK"] * len(responses)
    assert len({row["batch_id"] for row in rows}) == 1
    assert [int(row["run"]) for row in rows] == responses


@pytest.mark.parametrize("quick", [False, True], ids=("normal", "quick"))
@pytest.mark.parametrize("with_guide", [False, True], ids=("fresh", "guide"))
def test_installed_wizard_submits_valid_answers(tmp_path, sandbox, quick, with_guide):
    env, guide = sandbox
    with terminal(tmp_path, env) as console:
        (_quick if quick else _normal)(console, guide if with_guide else None)
        if not quick:
            console.expect(r"(?m)^> Submit 1 response")
            console.write(ENTER)
        console.expect("Finished: 1/1 submitted")
        console.exit()
    _assert_rows(tmp_path, [1])
    [generation] = _trace(tmp_path, "generate")
    assert generation["instructions"] == INSTRUCTIONS
    if with_guide:
        assert generation["guide"]["example_profiles"] == [{"1": 1, "2": "Example from the guide CSV."}]
    else:
        assert generation["guide"] is None


def test_installed_wizard_rejects_unrelated_csv_before_preparation(tmp_path, sandbox):
    env, _ = sandbox
    data = tmp_path / "unrelated.csv"
    data.write_text("Unrelated header\nNo answer\n", encoding="utf-8")
    with terminal(tmp_path, env) as console:
        _start(console, quick=False)
        console.answer(DOWN, "Copy a prompt to create your answer CSV")
        console.answer("", CSV_PROMPT)
        console.answer(str(data), "No CSV headers match this form")
        console.expect(SOURCES)
    assert not _trace(tmp_path, "generate")
    assert not _trace(tmp_path, "submit")


@pytest.mark.parametrize("quick", [False, True], ids=("normal", "quick"))
def test_installed_wizard_keeps_incomplete_generation_without_submitting(tmp_path, sandbox, quick):
    env, _ = sandbox
    with terminal(tmp_path, {**env, "GFORMBOT_WIZARD_FIXTURE_INCOMPLETE": "1"}) as console:
        (_quick if quick else _normal)(console, count="3")
        console.expect(r"Generation paused\. Prepared 2/3 responses")
        console.expect(r"(?m)^> Back to Start \(keep draft\)")
        console.write(ENTER)
        console.exit()

    assert not _trace(tmp_path, "submit")
    assert not _trace(tmp_path, "review")
    assert not list(tmp_path.glob("gformbot-responses*.json"))
    [draft_path] = (tmp_path / "state" / "gformbot" / "ai-drafts").glob("*.json")
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["config"]["run_settings"]["count"] == 3
    profiles = draft["state"]["profiles"]
    assert [profile["position"] for profile in profiles] == [1, 0, 1]
    assert [profiles[index]["answers"]["2"] for index in (0, 2)] == [
        "Fixture answer 1 records a distinct handoff.",
        "Fixture answer 3 records a distinct handoff.",
    ]
    generation = _trace(tmp_path, "generate")
    assert generation[0]["ids"] == [1, 2, 3]
    assert len(generation) > 1 and all(call["ids"] == [2] for call in generation[1:])


def test_installed_wizard_resumes_complete_batch_after_interrupt(tmp_path, sandbox):
    env, _ = sandbox
    with terminal(
        tmp_path,
        {**env, "GFORMBOT_WIZARD_FIXTURE_INTERRUPT": "1"},
    ) as console:
        _quick(console, count="3", invalid=True)
        console.expect("Response 1 submitted")
        console.write("\x03")
        console.expect("Cancelled\\.")
        console.expect()
    _assert_rows(tmp_path, [1])
    before = _trace(tmp_path, "generate")
    assert [call["ids"] for call in before] == [[1, 2, 3]]
    with terminal(tmp_path, env) as console:
        console.expect(START)
        console.answer(DOWN, r"(?m)^> 1/3 submitted")
        console.answer("", r"(?m)^> Submit 2 responses")
        console.write(ENTER)
        console.expect("Finished: 3/3 submitted")
        console.exit()
    _assert_rows(tmp_path, [1, 2, 3])
    assert _trace(tmp_path, "generate") == before


def test_installed_wizard_ignores_enter_queued_during_submission(tmp_path, sandbox):
    env, _ = sandbox
    with terminal(tmp_path, {**env, "GFORMBOT_WIZARD_FIXTURE_QUEUED_ENTER": "1"}) as console:
        _quick(console, count="2")
        console.expect("Response 1 submitted")
        console.expect(r"Fixture waiting for a queued key\.")
        console.write(ENTER)
        console.expect("Finished: 2/2 submitted")
        console.expect(START)
        # A new Down key must still select Continue batches in the Start menu.
        console.write(DOWN)
        console.expect(r"(?m)^>[ \t]*Continue batches")
        console.write(DOWN * 2 + ENTER)
        console.expect()
    _assert_rows(tmp_path, [1, 2])
