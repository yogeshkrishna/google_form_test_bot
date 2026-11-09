"""Interactive prompts require a console, including on Windows NUL handles."""

import io
import os
import sys
from types import SimpleNamespace

import pytest

from gformbot import terminal


class Stream:
    closed = False

    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty

    def fileno(self):
        return 17


def platform_stream(monkeypatch, stream, platform):
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(stdin=stream, platform=platform))


def windows_console(monkeypatch, result):
    calls = []
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 100))

    def console_mode(handle, mode):
        calls.append(handle.value)
        return result

    monkeypatch.setattr(
        terminal.ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetConsoleMode=console_mode)),
        raising=False,
    )
    return calls


def test_posix_tty_supports_interactive_input(monkeypatch):
    platform_stream(monkeypatch, Stream(), "linux")
    assert terminal.interactive_input()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_pipe_is_not_interactive(monkeypatch, platform):
    platform_stream(monkeypatch, Stream(tty=False), platform)
    assert not terminal.interactive_input()


def test_windows_nul_is_not_interactive_even_when_isatty_is_true(monkeypatch):
    platform_stream(monkeypatch, Stream(), "win32")
    calls = windows_console(monkeypatch, 0)
    assert not terminal.interactive_input()
    assert calls == [117]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows character-device behavior")
def test_native_windows_nul_is_not_interactive():
    with open(os.devnull) as stream:
        assert not terminal.interactive_input(stream)


def test_windows_console_supports_interactive_input(monkeypatch):
    platform_stream(monkeypatch, Stream(), "win32")
    calls = windows_console(monkeypatch, 1)
    assert terminal.interactive_input()
    assert calls == [117]


def test_windows_stream_without_descriptor_is_not_interactive(monkeypatch):
    platform_stream(monkeypatch, SimpleNamespace(isatty=lambda: True), "win32")
    assert not terminal.interactive_input()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_closed_input_is_not_interactive(monkeypatch, platform):
    stream = io.StringIO()
    stream.close()
    platform_stream(monkeypatch, stream, platform)
    assert not terminal.interactive_input()


def test_unavailable_or_explicitly_missing_stream_is_not_interactive(monkeypatch):
    platform_stream(monkeypatch, None, "linux")
    assert not terminal.interactive_input()
    platform_stream(monkeypatch, Stream(), "linux")
    assert not terminal.interactive_input(None)


def test_explicit_output_stream_can_disable_rich_prompts(monkeypatch):
    platform_stream(monkeypatch, Stream(), "linux")
    assert not terminal.interactive_input(Stream(tty=False))


@pytest.mark.parametrize(
    ("keys", "expected"),
    [("x\x7f\x7f3\r", "3"), ("x\x7f\x7f\x7f\r", None), ("\x7f3\x7f\x7f\r", "")],
)
def test_backspace_requires_two_presses_after_text_is_empty(keys, expected):
    from InquirerPy import inquirer
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    token = terminal.wizard_navigation.set(True)
    try:
        with create_pipe_input() as pipe:
            prompt = inquirer.text(message="Number", input=pipe, output=DummyOutput())
            pipe.send_text(keys)
            if expected is None:
                with pytest.raises(terminal.PromptBack):
                    terminal.run_prompt(prompt, typing=True)
            else:
                assert terminal.run_prompt(prompt, typing=True) == expected
    finally:
        terminal.wizard_navigation.reset(token)
