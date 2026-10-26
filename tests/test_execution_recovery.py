from types import SimpleNamespace

import pytest

from gformbot import browser, filler
from gformbot.execution import Runner


class ReviewWindows:
    def __init__(self):
        self.current_url = "https://example.test/form"
        self.window_handles = ["first"]
        self.current_window_handle = "first"
        self.switch_to = self
        self.closed = False
        self.switches = []

    def window(self, handle):
        assert handle in self.window_handles, "A closed tab must not be selected."
        self.current_window_handle = handle
        self.switches.append(handle)

    def new_window(self, kind):
        assert kind == "tab"
        self.window_handles.append("second")
        self.window("second")

    def execute_script(self, *args):
        pass

    def quit(self):
        self.closed = True


def test_selecting_closed_background_response_keeps_other_review_edits(monkeypatch):
    driver = ReviewWindows()
    choices = iter(["open", 1, "open", 2, "open", 1, "stop"])
    opens = []
    messages, reviews, failures, fills = [], [], [], []

    def select(*args, **kwargs):
        value = next(choices)
        if value == "open":
            opens.append(True)
            if len(opens) == 3:
                driver.window_handles.remove("first")
        return value

    ui = SimpleNamespace(_ask_select=select, _choice=lambda *args: args, _info=messages.append)
    config = SimpleNamespace(
        browser="chrome", confirmation_text="", url="https://example.test/form", form_title="Review"
    )
    session = SimpleNamespace(
        ids=[7, 9], review=reviews.append, failed=lambda index, uncertain: failures.append((index, uncertain))
    )
    monkeypatch.setattr(browser, "get_driver", lambda *a, **kw: driver)

    def fill(config, row, **options):
        options["before_review"]()
        fills.append(row)
        return True, "filled"

    monkeypatch.setattr(filler, "submit_response", fill)
    monkeypatch.setattr(filler, "_positive_confirmation", lambda *a: False)
    answers = [{1: "First"}, {1: "Second, edited manually in its own tab"}]
    assert Runner(config, answers, ui, session).background({1: 1, 2: 2}) is False
    assert fills == answers and reviews == [1, 2]
    assert failures == [(1, True)]
    assert driver.closed
    assert any("Response 7 closed" in message for message in messages)


def test_failed_review_browser_launch_does_not_mark_unseen_answers_uncertain(monkeypatch):
    reviews = []
    ui = SimpleNamespace(
        _ask_select=lambda *a, **kw: "open", _choice=lambda *args: args, _info=lambda message: None
    )
    config = SimpleNamespace(browser="chrome")
    session = SimpleNamespace(ids=[7], review=reviews.append)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Browser is unavailable")

    monkeypatch.setattr(browser, "get_driver", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        Runner(config, [{1: "Unseen"}], ui, session).background({1: 1})
    assert reviews == []
