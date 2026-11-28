"""Interactive prompts and progress displays."""

from __future__ import annotations

import ctypes
import sys
from contextvars import ContextVar
from math import ceil
from threading import Event, Lock, Thread
from time import monotonic

_DEFAULT_STREAM = object()
_PROGRESS_HEARTBEAT_SECONDS = 20
_GENERATION_PHASES = {
    "generate": "Creating answers",
    "validate": "Checking answer rules",
    "review": "Checking linked answers",
    "repair": "Repairing flagged answers",
}
wizard_navigation = ContextVar("wizard_navigation", default=False)


class PromptBack(Exception):
    """Return one step in the guided CLI."""


def _elapsed_text(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{hours}:" if hours else "") + f"{minutes:02d}:{seconds:02d}"


class GenerationProgress:
    """Keep saved counts and one clock visible while generation runs."""

    def __init__(self, console, total, *, saved=0, live=True, report=None):
        from rich.live import Live

        self._answers = (saved, total)
        self._review = None
        self._detail = ""
        self._phase = "generate"
        self._eta_stage = "answers" if saved < total else None
        self._sample = ("answers", saved, 0.0)
        self._deadline = None
        self._ended = None
        self._completed = False
        self._started = 0.0
        self._lock = Lock()
        self._live = (
            Live(
                console=console,
                get_renderable=self._render,
                refresh_per_second=1,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            if live
            else None
        )
        self._report = report or console.print
        if not live:
            self._stop = Event()
            self._heartbeat = Thread(target=self._plain_heartbeat, name="gformbot-progress", daemon=True)

    def __enter__(self):
        self._started = monotonic()
        self._sample = ("answers", self._answers[0], self._started)
        if self._live is None:
            self._report(self._phase_status())
            self._heartbeat.start()
        else:
            self._live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self._lock:
            self._ended = monotonic()
            self._completed = exc_type is None
        if self._live is None:
            self._stop.set()
            self._heartbeat.join()
            label = "AI work finished" if self._completed else "AI work stopped"
            self._report(f"{label} · Elapsed {_elapsed_text(self._ended - self._started)}")
        else:
            self._live.stop()

    def phase(self, phase):
        with self._lock:
            if phase == self._phase:
                return
            self._phase = phase
        if self._live is None:
            self._report(self._phase_status())
        else:
            self._live.refresh()

    def _phase_status(self):
        with self._lock:
            return f"{_GENERATION_PHASES[self._phase]} · Elapsed {_elapsed_text(monotonic() - self._started)}"

    def _plain_heartbeat(self):
        # Plain terminals cannot redraw a line. Print infrequently to keep logs readable.
        while not self._stop.wait(_PROGRESS_HEARTBEAT_SECONDS):
            self._report(self._phase_status())

    def answers(self, saved, total):
        if self._live is None:
            self._report(f"Creating answers: {saved}/{total} saved")
        else:
            self.update("answers", saved, total)

    def update(self, stage, saved, total):
        with self._lock:
            if stage in {"routes", "fields"}:
                detail = f"{saved}/{total} {stage} saved"
                if detail == self._detail:
                    return
                self._detail = detail
                self._estimate(stage, saved, total)
            elif stage == "answers":
                if self._answers == (saved, total):
                    return
                self._answers = (saved, total)
                if not self._detail or saved == total:
                    self._estimate(stage, saved, total)
            else:
                if self._review == (saved, total):
                    return
                reset = self._review is not None and saved < self._review[0]
                self._review = (saved, total)
                self._estimate(stage, saved, total, reset=reset)
        self._live.refresh()

    def _estimate(self, stage, saved, total, *, reset=False):
        self._eta_stage = stage if saved < total else None
        self._deadline = None
        now = monotonic()
        previous, first_saved, started = self._sample
        if reset or previous != stage or saved < first_saved:
            self._sample = (stage, saved, now)
        elif first_saved < saved < total and now > started:
            self._deadline = now + (now - started) * (total - saved) / (saved - first_saved)

    def _render(self):
        from rich.console import Group
        from rich.text import Text

        with self._lock:
            saved, total = self._answers
            answers = Text(
                f"Creating answers: {saved}/{total} saved",
                style="bright_black" if saved == total else "cyan",
                no_wrap=True,
                overflow="crop",
            )
            if saved < total and self._detail:
                answers.append(f"  |  {self._detail}", style="bright_black")
            lines = [answers]
            if self._review is not None:
                checked, target = self._review
                lines.append(
                    Text(
                        f"Quality checked: {checked}/{target} saved",
                        style="bright_black" if self._completed else "cyan",
                        no_wrap=True,
                        overflow="crop",
                    )
                )
            now = self._ended if self._ended is not None else monotonic()
            footer = f"Elapsed {_elapsed_text(now - self._started)}"
            if self._ended is None and self._phase != "generate":
                footer += f"  |  {_GENERATION_PHASES[self._phase]}"
            if self._ended is None and self._eta_stage and self._phase in {"generate", "review"}:
                estimate = ceil(self._deadline - now) if self._deadline is not None else 0
                remaining = f"~{_elapsed_text(estimate)}" if estimate > 0 else "estimating..."
                footer += f"  |  {self._eta_stage.title()} ETA {remaining}"
            lines.append(Text(footer, style="bright_black", no_wrap=True, overflow="crop"))
        return Group(*lines)


def run_prompt(prompt, *, typing=False):
    if wizard_navigation.get():
        armed_at = None

        @prompt.register_kb("c-h")
        def backspace(event):
            nonlocal armed_at
            if typing and event.current_buffer.text:
                event.current_buffer.delete_before_cursor(count=event.arg)
                armed_at = None
                return
            now = monotonic()
            if event.is_repeat and armed_at is not None and now - armed_at <= 0.65:
                prompt.status["skipped"] = True
                event.app.exit(exception=PromptBack())
                return
            armed_at = now

    return prompt.execute()


def prompt_steps(*steps):
    """Collect fields in order, returning to the preceding field on Back."""
    answers = []
    while len(answers) < len(steps):
        try:
            answer = steps[len(answers)](answers)
        except PromptBack:
            if not answers:
                raise
            answers.pop()
        else:
            answers.append(answer)
    return answers


def accent_choice(prompt, value):
    """Style the start shortcut in both highlighted and unhighlighted rows."""
    control = prompt.content_control

    def decorate(render):
        def render_choice(choice):
            parts = render(choice)
            if choice["value"] == value:
                return [
                    (style + " fg:#d3a6ff bold" if text == choice["name"] else style, text)
                    for style, text in parts
                ]
            return parts

        return render_choice

    control._get_normal_text = decorate(control._get_normal_text)
    control._get_hover_text = decorate(control._get_hover_text)


def interactive_input(stream=_DEFAULT_STREAM):
    """Check the current input, or an explicit stream, for a usable console.

    Windows reports character devices such as NUL as TTYs too. Requiring a
    console mode keeps redirected input on the numbered prompt path.
    """
    if stream is _DEFAULT_STREAM:
        stream = getattr(sys, "stdin", None)
    try:
        if stream is None or getattr(stream, "closed", False) or not stream.isatty():
            return False
        if sys.platform != "win32":
            return True
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False


def discard_pending_input():
    """Drop keys queued on a terminal while the wizard was busy."""
    if not interactive_input():
        return
    try:
        fd = sys.stdin.fileno()
        if sys.platform == "win32":
            import msvcrt

            handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
            ctypes.windll.kernel32.FlushConsoleInputBuffer(handle)
        else:
            import termios

            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            except termios.error:
                pass  # The terminal can close between the console check and the flush.
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        pass
