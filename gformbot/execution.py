"""Run exact profiles with explicit review and durable submission boundaries."""

from __future__ import annotations

import random
import sys
import time


class Runner:
    def __init__(self, config, answers, ui, session=None):
        self.config, self.answers, self.ui, self.session = config, answers, ui, session

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

    def review_mode(self, submit):
        c, ui = self.config, self.ui
        if submit or c.headless or c.browser == "headless" or not sys.stdin.isatty():
            return "check"
        return ui._ask_select(
            "Review responses",
            [
                ui._choice("wait", "One at a time: review and submit before the next"),
                ui._choice(
                    "tabs",
                    "Prepare all; open chosen responses when ready"
                    if c.background
                    else "Separate private tabs",
                ),
            ],
            default="wait",
        )

    def wait_manual(self, driver, pending, *, tabs=False):
        from selenium.common.exceptions import WebDriverException

        from .filler import _positive_confirmation

        ui, c = self.ui, self.config
        ui._info("Submit when ready. Ctrl+C offers continue or close-and-keep options.")
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
                            f"Response {self.original(index)} closed before confirmation. Reconcile it before retrying."
                        )
                        continue
                    if tabs:
                        driver.switch_to.window(handle)
                    if _positive_confirmation(driver, c.confirmation_text, c.url):
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
                    "Review browser closed. Unconfirmed responses require reconciliation before retrying."
                )
                return False
        return all_confirmed

    def background(self, pending):
        from selenium.common.exceptions import WebDriverException

        from .browser import get_driver
        from .filler import _positive_confirmation, submit_response

        ui, c = self.ui, self.config
        viewer, opened = None, {}

        def check():
            if viewer is None:
                return
            for handle, index in list(opened.items()):
                if handle not in viewer.window_handles:
                    if self.session:
                        self.session.failed(index, uncertain=True)
                    del opened[handle]
                    ui._info(
                        f"Response {self.original(index)} closed without confirmation; it must be reconciled before retry."
                    )
                    continue
                viewer.switch_to.window(handle)
                if _positive_confirmation(viewer, c.confirmation_text, c.url):
                    self.confirm(index)
                    pending.pop(index, None)
                    del opened[handle]

        try:
            while pending:
                choice = ui._ask_select(
                    f"{len(pending)} response(s) prepared",
                    [
                        ui._choice("open", "Open a response to review, edit or submit"),
                        ui._choice("check", "Check submissions"),
                        ui._choice("stop", "Exit and keep remaining responses saved"),
                    ],
                    default="open",
                )
                if choice == "stop":
                    check()
                    return not pending
                if choice == "check":
                    check()
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
                        check()
                        continue
                    viewer.switch_to.window(existing)
                else:
                    if viewer is None:
                        viewer = get_driver(c.browser, headless=False)
                    elif opened:
                        viewer.switch_to.new_window("tab")
                    if self.session:
                        self.session.review(index)
                    ui._info(
                        f"Opening response {self.original(index)}. Saved answers remain unchanged unless you edit the batch."
                    )
                    ok, message = submit_response(c, self.answers[index - 1], submit=False, driver=viewer)
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
            ui._info("Review browser disconnected. Remaining responses are kept for reconciliation.")
            return False
        finally:
            if viewer is not None:
                try:
                    viewer.quit()
                except Exception as exc:
                    ui._info(f"Could not close the review browser: {exc}")

    def run(self, *, dry_run=False, submit=False):
        from .browser import get_driver
        from .config import clean_path, validate_config
        from .filler import SubmissionRejectedError, SubmissionUncertainError, submit_response
        from .generator import validate_answer
        from .inspector import inspect_form, schema_fingerprint
        from .logger import validate_log_path

        c, ui = self.config, self.ui
        validate_config(c)
        self.answers = [validate_answer(c, a) for a in self.answers]
        if len(self.answers) != c.count:
            raise ValueError("Response count does not match prepared answers.")
        if dry_run:
            ui._info("Preview only; no browser was opened.")
            return False
        validate_log_path(c.log_file)
        current = ui.config_from_schema(inspect_form(c.url))
        if schema_fingerprint(current) != schema_fingerprint(c):
            raise ValueError("The form has changed since inspection. Prepare a new response plan.")
        if getattr(c, "_instructions_known", True) and current.instructions != c.instructions:
            raise ValueError(
                "The form instructions have changed since inspection. Prepare a new response plan."
            )
        review = self.review_mode(submit)
        # Quiet review needs no hidden browser pass. Open fills each requested profile once.
        if not submit and c.background and review != "check":
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
        ui._section("Submitting responses" if submit else "Testing or reviewing form filling")
        driver = get_driver(c.browser, headless=c.headless or c.background)
        completed, pending = 0, {}
        try:
            for index, answer in enumerate(self.answers, 1):
                try:
                    if review == "tabs" and index > 1:
                        driver.switch_to.new_window("tab")
                    if not submit and review != "check" and self.session:
                        self.session.review(index)
                    options = {"submit": submit, "driver": driver}
                    if submit and self.session:
                        options["before_submit"] = lambda i=index: self.session.before_submit(i)
                    ok, message = submit_response(c, answer, **options)
                    if not ok:
                        if self.session and submit:
                            self.session.failed(index, uncertain=False)
                        self.log(index, False, message)
                        raise ValueError(message)
                    if submit and self.session:
                        self.session.confirm(index)
                        c._plan_digest = self.session.digest
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
        finally:
            try:
                driver.quit()
            except Exception as exc:
                ui._info(f"Could not close the private browser: {exc}")
        ui._info(f"Finished: {completed}/{len(self.answers)} {mode}. Log: {clean_path(c.log_file).resolve()}")
        if not submit:
            ui._info("No responses were submitted by the tool. The batch is kept for later use.")
        return submit or review != "check"
