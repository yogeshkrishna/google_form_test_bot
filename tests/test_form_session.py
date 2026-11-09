"""Account restrictions stop before any response request is opened."""

from types import SimpleNamespace

import pytest

from gformbot import transport
from gformbot.access import FormAccessError
from gformbot.config import QuestionConfig, RunConfig
from gformbot.execution import Runner


@pytest.mark.parametrize("restriction", ["requires_signin", "one_response", "email_collection"])
def test_runner_rejects_account_forms_before_http_submission(tmp_path, monkeypatch, restriction):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/session_fixture/viewform",
        questions=[QuestionConfig(1, "Answer", "text", True, entry_id="1")],
        log_file=str(tmp_path / "runs.csv"),
    )
    setattr(config, restriction, "verified" if restriction == "email_collection" else True)
    monkeypatch.setattr(transport, "Client", lambda *a: pytest.fail("Account form opened a client"))
    with pytest.raises(FormAccessError, match="without an account"):
        Runner(config, [{1: "Fictional answer"}], SimpleNamespace()).run()
