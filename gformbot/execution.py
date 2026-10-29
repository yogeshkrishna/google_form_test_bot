"""Run exact profiles with explicit review and durable submission boundaries."""

from __future__ import annotations

import random
import sys
import time

from .config import config_from_schema


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

    def observer(self, index):
        return lambda driver, section, stage: self.followups.observe(
            driver, self.original(index), section, stage
        )

    def original(self, index):
        return self.session.ids[index - 1] if self.session else index

    def log(self, index, ok, message):
        from .logger import log_result

        context = {"batch_id": self.session.identity, "form_url": self.config.url} if self.session else {}
        log_result(
            self.config.log_file, self.original(index), self.answers[index - 1], ok, message, **context
        )

    def confirm(self, index):
        if self.session:
            self.session.confirm(index)
            self.config._plan_digest = self.session.digest  # Legacy private callback compatibility.
        self.log(index, True, "submitted manually")

    def confirmed(self, driver, index):
        from .access import FormAccessError, check_browser_access
        from .filler import _positive_confirmation

        try:
            check_browser_access(driver)
        except FormAccessError as exc:
            if self.session:
                self.session.failed(index, uncertain=True)
            self.ui._info(f"Response {self.original(index)} stopped: {exc} Its answers remain saved.")
            return None
        confirmed = _positive_confirmation(driver, self.config.confirmation_text, self.config.url)
        if confirmed:
            self.followups.observe(driver, self.original(index), 0, "confirmation")
        return confirmed

    def review_mode(self, submit):
        c, ui = self.config, self.ui
        if submit or not sys.stdin.isatty():
            return "check"
        return ui._ask_select(
            "How would you like to review the responses?",
            [
                ui._choice("wait", "One at a time: review and submit before the next"),
                ui._choice(
                    "tabs",
                    "Choose which responses to open, whenever I'm ready"
                    if c.background
                    else "Open each response in a separate private tab",
                ),
            ],
            default="wait",
        )

    def wait_manual(self, driver, pending, *, tabs=False):
        from .selenium_runtime import WebDriverException

        ui = self.ui
        ui._info(
            "Click Submit in the browser when ready. Press Ctrl+C to choose whether to wait or save and exit."
        )
        all_confirmed = True
        while pending:
            try:
                if tabs:
                    ui._ask_text(
                        f"{len(pending)} awaiting confirmation. Review/submit, then press Enter to check"
                    )
                handles = driver.window_handles
                for handle, index in list(pending.items()):
                    if handle not in handles:
                        all_confirmed = False
                        if self.session:
                            self.session.failed(index, uncertain=True)
                        del pending[handle]
                        ui._info(
                            f"Response {self.original(index)} closed before confirmation. Check whether it was submitted before retrying."
                        )
                        continue
                    if tabs:
                        driver.switch_to.window(handle)
                    status = self.confirmed(driver, index)
                    if status is None:
                        all_confirmed = False
                        del pending[handle]
                    elif status:
                        self.confirm(index)
                        del pending[handle]
                        ui._info(f"Response {self.original(index)}: manual submission confirmed.")
                if tabs and pending:
                    driver.switch_to.window(next(iter(pending)))
                if pending:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                if (
                    ui._ask_select(
                        "Responses are still open",
                        [
                            ui._choice("wait", "Keep waiting"),
                            ui._choice("stop", "Close and keep unconfirmed responses saved"),
                        ],
                        default="wait",
                    )
                    == "stop"
                ):
                    return False
            except WebDriverException:
                ui._info(
                    "The browser closed. Your answers are saved. Check whether they were submitted before retrying."
                )
                return False
        return all_confirmed

    def background(self, pending):
        from .browser import get_driver
        from .filler import submit_response
        from .selenium_runtime import WebDriverException

        ui, c = self.ui, self.config
        viewer, opened = None, {}

        def check():
            if viewer is None:
                return True
            accessible = True
            for handle, index in list(opened.items()):
                if handle not in viewer.window_handles:
                    if self.session:
                        self.session.failed(index, uncertain=True)
                    del opened[handle]
                    ui._info(
                        f"Response {self.original(index)} closed without confirmation. Check whether it was submitted before retrying."
                    )
                    continue
                viewer.switch_to.window(handle)
                status = self.confirmed(viewer, index)
                if status is None:
                    accessible = False
                    del opened[handle]
                elif status:
                    self.confirm(index)
                    pending.pop(index, None)
                    del opened[handle]
            return accessible

        try:
            while pending:
                choice = ui._ask_select(
                    f"{len(pending)} response(s) prepared",
                    [
                        ui._choice("open", "Open a response to review, edit or submit"),
                        ui._choice("check", "Check which responses were submitted"),
                        ui._choice("stop", "Exit and keep remaining responses saved"),
                    ],
                    default="open",
                )
                if choice == "stop":
                    return check() and not pending
                if choice == "check":
                    if not check():
                        return False
                    continue
                index = (
                    next(iter(pending))
                    if len(pending) == 1
                    else ui._ask_select(
                        "Open which response?",
                        [
                            ui._choice(
                                i,
                                f"Response {self.original(i)}"
                                + (" (already open; keep edits)" if i in opened.values() else ""),
                            )
                            for i in pending
                        ]
                        + [ui._choice("back", "Back")],
                    )
                )
                if index == "back":
                    continue
                existing = next((h for h, i in opened.items() if i == index), None)
                if existing:
                    if existing not in viewer.window_handles:
                        if not check():
                            return False
                        continue
                    viewer.switch_to.window(existing)
                else:
                    if viewer is None:
                        viewer = get_driver(c.browser)
                    elif opened:
                        viewer.switch_to.new_window("tab")
                    ui._info(
                        f"Opening response {self.original(index)}. Saved answers remain unchanged unless you edit the batch."
                    )
                    options = {"submit": False, "driver": viewer, "observe": self.observer(index)}
                    if self.session:
                        options["before_review"] = lambda i=index: self.session.review(i)
                    ok, message = submit_response(c, self.answers[index - 1], **options)
                    if not ok:
                        raise ValueError(message)
                    viewer.execute_script(
                        "document.title = arguments[0]", f"Response {self.original(index)} — {c.form_title}"
                    )
                    opened[viewer.current_window_handle] = index
                if len(pending) == 1:
                    confirmed = self.wait_manual(viewer, opened)
                    if confirmed:
                        pending.clear()
                    return confirmed
            return True
        except WebDriverException:
            ui._info(
                "The browser disconnected. Remaining answers are saved. Check whether they were submitted before retrying."
            )
            return False
        finally:
            if viewer is not None:
                try:
                    viewer.quit()
                except Exception as exc:
                    ui._info(f"Could not close the review browser: {exc}")

    def wait_halt(self, driver, pending):
        """Keep every review window alive until it is closed or the user stops."""
        from .selenium_runtime import (
            NoSuchWindowException,
            StaleElementReferenceException,
            WebDriverException,
        )

        ui = self.ui
        unconfirmed_closed = False
        ui._info(
            "The forms are filled. Review or edit them and click Submit yourself if you choose. "
            "Close the browser or press Ctrl+C when finished."
        )

        def closed(handle):
            nonlocal unconfirmed_closed
            index = pending.pop(handle)
            unconfirmed_closed = True
            if self.session:
                self.session.failed(index, uncertain=True)
            ui._info(
                f"Response {self.original(index)} closed before submission was confirmed. "
                "Its saved answers are kept; check whether it was submitted before retrying."
            )

        while True:
            try:
                handles = driver.window_handles
                for handle in list(pending):
                    if handle not in handles:
                        closed(handle)
                if not handles:
                    return not pending and not unconfirmed_closed
                try:
                    previous = driver.current_window_handle
                except WebDriverException:
                    previous = None
                selected = previous
                ordered = sorted(list(pending.items()), key=lambda item: item[0] != previous)
                for handle, index in ordered:
                    try:
                        if selected != handle:
                            driver.switch_to.window(handle)
                            selected = handle
                        status = self.confirmed(driver, index)
                        if status is None:
                            unconfirmed_closed = True
                            del pending[handle]
                        elif status:
                            self.confirm(index)
                            del pending[handle]
                            ui._info(f"Response {self.original(index)}: manual submission confirmed.")
                    except NoSuchWindowException:
                        if handle not in driver.window_handles:
                            closed(handle)
                        else:
                            raise
                    except StaleElementReferenceException:
                        continue  # A manual submission may replace the document during a scan.
                if previous in driver.window_handles and selected != previous:
                    driver.switch_to.window(previous)
                time.sleep(1)
            except KeyboardInterrupt:
                ui._info("Stopped. Unconfirmed responses remain saved.")
                return not pending and not unconfirmed_closed
            except WebDriverException:
                for handle in list(pending):
                    closed(handle)
                ui._info("The browser closed. Unconfirmed responses remain saved.")
                return not pending and not unconfirmed_closed

    def run(self, *, dry_run=False, submit=False, halt=False, compact=False, check_only=False):
        try:
            return self._run(
                dry_run=dry_run, submit=submit, halt=halt, compact=compact, check_only=check_only
            )
        finally:
            self.followups.show(self.ui)

    def _http(self, *, submit, compact=False):
        from .access import SubmissionRejectedError, SubmissionUncertainError
        from .config import clean_path
        from .transport import Client

        c, ui = self.config, self.ui
        mode = "submitted" if submit else "checked"
        if not compact:
            ui._section("Sending responses" if submit else "Checking prepared requests")
        completed = 0
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

    def _run(self, *, dry_run=False, submit=False, halt=False, compact=False, check_only=False):
        from .access import require_public_form
        from .config import clean_path, validate_config
        from .generator import validate_answer
        from .inspector import inspect_form, schema_fingerprint
        from .logger import validate_log_path
        from .routing import Routing

        c, ui = self.config, self.ui
        if halt:
            submit = False
        validate_config(c)
        c._routing = Routing(c)
        self.answers = [validate_answer(c, a, routing=c._routing) for a in self.answers]
        if len(self.answers) != c.count:
            raise ValueError("Response count does not match prepared answers.")
        if dry_run:
            ui._info("Preview only; no browser was opened.")
            return False
        validate_log_path(c.log_file)
        require_public_form(c)
        if check_only or submit or (not halt and not sys.stdin.isatty()):
            return self._http(submit=submit, compact=compact)
        from .browser import get_driver
        from .filler import SubmissionRejectedError, SubmissionUncertainError, submit_response

        current = config_from_schema(inspect_form(c.url))
        require_public_form(current)
        if schema_fingerprint(current) != schema_fingerprint(c):
            raise ValueError("The form has changed since inspection. Prepare a new response plan.")
        if getattr(c, "_instructions_known", True) and current.instructions != c.instructions:
            raise ValueError(
                "The form instructions have changed since inspection. Prepare a new response plan."
            )
        c.requires_signin = current.requires_signin
        c.one_response = current.one_response
        c.email_collection = current.email_collection
        review = "halt" if halt else self.review_mode(submit)
        # Quiet review opens each requested profile once, only on demand.
        if not halt and not submit and c.background and review != "check":
            ui._info(f"Prepared {len(self.answers)} exact responses. No window opens until you choose Open.")
            if review == "tabs":
                return self.background({i: i for i in range(1, len(self.answers) + 1)})
            for index in range(1, len(self.answers) + 1):
                if not self.background({index: index}):
                    return False
            return True
        if (
            review == "tabs"
            and c.count > 20
            and not ui._ask_confirm(f"Open {c.count} tabs? Choose No to review one at a time.")
        ):
            review = "wait"
        mode = "submitted" if submit else "filled"
        if not compact:
            ui._section("Submitting responses" if submit else "Testing or reviewing form filling")
        driver = get_driver(c.browser)
        completed, pending = 0, {}
        try:
            for index, answer in enumerate(self.answers, 1):
                try:
                    if review in {"tabs", "halt"} and index > 1:
                        driver.switch_to.new_window("tab")
                    options = {"submit": submit, "driver": driver, "observe": self.observer(index)}
                    if self.session and review != "check":
                        options["before_review"] = lambda i=index: self.session.review(i)
                    ok, message = submit_response(c, answer, **options)
                    if not ok:
                        self.log(index, False, message)
                        raise ValueError(message)
                    completed += 1
                    self.log(index, True, message)
                    ui._info(f"[{completed}/{len(self.answers)}] Response {self.original(index)} {mode}")
                    if review != "check":
                        pending[driver.current_window_handle] = index
                        if review == "wait" and not self.wait_manual(driver, pending):
                            return False
                except SubmissionUncertainError as exc:
                    if self.session:
                        self.session.failed(index, uncertain=True)
                    self.log(index, False, str(exc))
                    raise ValueError(
                        f"Response {self.original(index)} is uncertain. Check the form before retrying: {exc}"
                    ) from exc
                except SubmissionRejectedError as exc:
                    if self.session:
                        self.session.failed(index, uncertain=False)
                    self.log(index, False, str(exc))
                    raise ValueError(
                        f"Response {self.original(index)} was rejected by form validation: {exc}"
                    ) from exc
                except Exception as exc:
                    raise ValueError(
                        f"Response {self.original(index)} failed after {completed}/{len(self.answers)} {mode}: {exc}"
                    ) from exc
                if index < len(self.answers) and c.max_delay:
                    time.sleep(random.uniform(c.min_delay, c.max_delay))
            if review == "tabs":
                return self.wait_manual(driver, pending, tabs=True)
            if halt:
                return self.wait_halt(driver, pending)
        finally:
            try:
                driver.quit()
            except Exception as exc:
                ui._info(f"Could not close the private browser: {exc}")
        ui._info(f"Finished: {completed}/{len(self.answers)} {mode}. Log: {clean_path(c.log_file).resolve()}")
        if not submit:
            ui._info("No responses were submitted by the tool. The batch is kept for later use.")
        return submit or review != "check"
