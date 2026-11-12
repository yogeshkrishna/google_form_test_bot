"""Detect streams that can support interactive console prompts."""

from __future__ import annotations

import ctypes
import sys
from contextvars import ContextVar
from time import monotonic

_DEFAULT_STREAM = object()
wizard_navigation = ContextVar("wizard_navigation", default=False)


class PromptBack(Exception):
    """Return one step in the guided CLI."""


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
