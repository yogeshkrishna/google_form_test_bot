"""Run exact profiles over HTTP with durable submission boundaries."""

from __future__ import annotations

import random
import time


class Runner:
    def __init__(self, config, answers, ui, session=None):
        self.config, self.answers, self.ui, self.session = config, answers, ui, session
        import uuid

        from .followups import Recorder
        from .state import Journal

        self.followups = Recorder(
            Journal(getattr(session, "identity", None) or str(uuid.uuid4())),
            getattr(config, "log_file", "gformbot-runs.csv"),
            getattr(config, "form_title", "Google Form"),
        )

    def original(self, index):
        return self.session.ids[index - 1] if self.session else index

    def log(self, index, ok, message):
        from .logger import log_result

        context = {"batch_id": self.session.identity, "form_url": self.config.url} if self.session else {}
        log_result(
            self.config.log_file, self.original(index), self.answers[index - 1], ok, message, **context
        )

    def run(self, *, submit=True, compact=False):
        from .access import require_public_form
        from .config import validate_config
        from .generator import validate_answer
        from .logger import validate_log_path
        from .routing import Routing

        try:
            c = self.config
            validate_config(c)
            c._routing = Routing(c)
            self.answers = [validate_answer(c, answer, routing=c._routing) for answer in self.answers]
            if len(self.answers) != c.count:
                raise ValueError("Response count does not match prepared answers.")
            validate_log_path(c.log_file)
            require_public_form(c)
            return self._http(submit=submit, compact=compact)
        finally:
            self.followups.show(self.ui)

    def _http(self, *, submit, compact=False):
        from .access import SubmissionRejectedError, SubmissionUncertainError
        from .config import clean_path
        from .transport import Client

        c, ui = self.config, self.ui
        mode = "submitted" if submit else "checked"
        if not compact:
            ui._section("Submitting responses" if submit else "Checking prepared requests")
        completed = 0
        if submit and self.session:
            self.session.start()
            c._plan_digest = self.session.digest
        with Client(c) as client:
            for index, answer in enumerate(self.answers, 1):
                try:

                    def observe(section, stage, blocks, i=index):
                        self.followups.record(blocks, self.original(i), section, stage, c.url)

                    try:
                        options = {"observe": observe}
                        if submit:
                            if self.session:
                                options["before_submit"] = lambda i=index: self.session.before_submit(i)
                            ok, message = client.submit(answer, **options)
                        else:
                            ok, message = client.validate(answer, **options)
                        if not ok:
                            raise SubmissionRejectedError(message)
                    except SubmissionUncertainError as exc:
                        if self.session:
                            self.session.failed(index, uncertain=True)
                        self.log(index, False, str(exc))
                        raise ValueError(
                            f"Response {self.original(index)} is uncertain. Check the form before retrying: {exc}"
                        ) from exc
                    except SubmissionRejectedError as exc:
                        if self.session and submit:
                            self.session.failed(index, uncertain=False)
                        self.log(index, False, str(exc))
                        raise ValueError(f"Response {self.original(index)} was rejected: {exc}") from exc
                    if submit and self.session:
                        self.session.confirm(index)
                        c._plan_digest = self.session.digest
                    completed += 1
                    self.log(index, True, message)
                    ui._info(f"[{completed}/{len(self.answers)}] Response {self.original(index)} {mode}")
                    if submit and index < len(self.answers) and c.max_delay:
                        time.sleep(random.uniform(c.min_delay, c.max_delay))
                except Exception as exc:
                    raise ValueError(
                        f"Response {self.original(index)} failed after {completed}/{len(self.answers)} {mode}: {exc}"
                    ) from exc
        ui._info(f"Finished: {completed}/{len(self.answers)} {mode}. Log: {clean_path(c.log_file).resolve()}")
        if not submit:
            ui._info(
                "Requests validated locally against the current form. Nothing was sent; answers stay saved."
            )
        return submit
