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

    def log(self, index, ok, message, *, uncertain=False):
        from .logger import log_result

        context = {"batch_id": self.session.identity, "form_url": self.config.url} if self.session else {}
        if uncertain:
            context["uncertain"] = True
        log_result(
            self.config.log_file,
            self.original(index),
            self.answers[index - 1],
            ok,
            message,
            form_title=self.config.form_title,
            **context,
        )

    def run(self, *, compact=False):
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
            return self._http(compact=compact)
        finally:
            self.followups.show(self.ui)

    def _http(self, *, compact=False):
        from .access import SubmissionRejectedError, SubmissionUncertainError
        from .config import clean_path
        from .transport import Client

        c, ui = self.config, self.ui
        if not compact:
            ui._section("Submitting responses")
        completed = 0
        if self.session:
            self.session.start()
            c._plan_digest = self.session.digest
        with Client(c) as client:
            for index, answer in enumerate(self.answers, 1):
                try:

                    def observe(section, stage, blocks, i=index):
                        self.followups.record(blocks, self.original(i), section, stage, c.url)

                    try:
                        options = {"observe": observe}
                        if self.session:
                            options["before_submit"] = lambda i=index: self.session.before_submit(i)
                        ok, message = client.submit(answer, **options)
                        if not ok:
                            raise SubmissionRejectedError(message)
                    except SubmissionUncertainError as exc:
                        if self.session:
                            self.session.failed(index, uncertain=True)
                        self.log(index, False, str(exc), uncertain=True)
                        raise ValueError(
                            f"Response {self.original(index)} is uncertain. Check the form before retrying: {exc}"
                        ) from exc
                    except SubmissionRejectedError as exc:
                        if self.session:
                            self.session.failed(index, uncertain=False)
                        self.log(index, False, str(exc))
                        raise ValueError(f"Response {self.original(index)} was rejected: {exc}") from exc
                    if self.session:
                        self.session.confirm(index)
                        c._plan_digest = self.session.digest
                    completed += 1
                    self.log(index, True, message)
                    ui._info(f"[{completed}/{len(self.answers)}] Response {self.original(index)} submitted")
                    if index < len(self.answers) and c.max_delay:
                        time.sleep(random.uniform(c.min_delay, c.max_delay))
                except Exception as exc:
                    raise ValueError(
                        f"Response {self.original(index)} failed after {completed}/{len(self.answers)} submitted: {exc}"
                    ) from exc
        ui._info(
            f"Finished: {completed}/{len(self.answers)} submitted. Log: {clean_path(c.log_file).resolve()}"
        )
        return True
