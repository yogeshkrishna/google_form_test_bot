"""Fill exact prepared responses with verified navigation and confirmation."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import urlparse

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .config import QuestionConfig, RunConfig
from .validation import parse_date

_SUPPORTED = {"radio", "checkbox", "dropdown", "scale", "text", "textarea", "date"}


class SubmissionUncertainError(RuntimeError):
    """Submit was clicked, but acceptance could not be confirmed."""


class SubmissionRejectedError(RuntimeError):
    """The form explicitly rejected an attempted submission."""


def _require_supported(config: RunConfig):
    if config.unsupported:
        raise RuntimeError("This form cannot be filled reliably: " + "; ".join(config.unsupported))
    if not config.questions:
        raise RuntimeError("No answerable questions are configured.")
    unknown = [q.text for q in config.questions if q.qtype not in _SUPPORTED]
    if unknown:
        raise RuntimeError("Unsupported controls: " + "; ".join(unknown))
    if not config.sections:
        config.sections = [f"Section {i + 1}" for i in range(max(q.page for q in config.questions))]


def _visible(elements):
    result = []
    for element in elements:
        try:
            if element.is_displayed():
                result.append(element)
        except StaleElementReferenceException:
            continue
    return result


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _heading(box) -> str:
    for selector in (".M7eMe", "[role='heading']", "legend"):
        elements = box.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return _normalized(elements[0].text).removesuffix(" *")
    return ""


def _question_boxes(driver):
    boxes = _visible(driver.find_elements(By.CSS_SELECTOR, "[role='listitem']"))
    # Checkbox options are themselves listitems in Google's current DOM.
    # Only the outer question listitem owns its entry identity and heading.
    boxes = [box for box in boxes if not box.find_elements(By.XPATH, "./ancestor::*[@role='listitem']")]
    return [
        box
        for box in boxes
        if box.find_elements(
            By.CSS_SELECTOR,
            "[role='radio'], [role='checkbox'], [role='listbox'], [role='combobox'], input:not([type='hidden']), textarea, select",
        )
    ]


def _page_questions(driver, form: RunConfig):
    # Read a box's identity once; avoid repeated browser round trips per candidate.
    by_entry, by_item, by_title = {}, {}, {}
    for question in form.questions:
        if question.entry_id:
            by_entry.setdefault("entry." + question.entry_id, []).append(question)
        if question.item_id:
            by_item.setdefault(question.item_id, []).append(question)
        by_title.setdefault(_normalized(question.text), []).append(question)
    matches, matched_indices = [], set()
    for box in _question_boxes(driver):
        names = [
            el.get_attribute("name") or "" for el in box.find_elements(By.CSS_SELECTOR, '[name^="entry."]')
        ]
        names = {re.sub(r"_(sentinel|year|month|day|hour|minute|second)$", "", name) for name in names}
        candidates = []
        if names and by_entry:
            for name in names:
                candidates.extend(by_entry.get(name, []))
        else:
            item_id = box.get_attribute("data-item-id")
            if not item_id:
                for element in [box] + box.find_elements(By.CSS_SELECTOR, "[data-params]"):
                    match = re.match(r"\s*%\.@\.\[\s*(\d+)", element.get_attribute("data-params") or "")
                    if match:
                        item_id = match.group(1)
                        break
            if item_id and by_item:
                candidates = by_item.get(str(item_id), [])
            else:
                candidates = by_title.get(_heading(box), [])
        unique = {question.index: question for question in candidates}
        if len(unique) != 1:
            raise RuntimeError(
                f"Unknown or ambiguous live question: {_heading(box)!r}. Inspect the form again."
            )
        question = next(iter(unique.values()))
        if question.index in matched_indices:
            raise RuntimeError(f"The live page repeats {question.text!r}; refusing ambiguous controls.")
        matched_indices.add(question.index)
        matches.append((question, box))
    return matches


def _control_value(element) -> str:
    return (
        element.get_attribute("data-value")
        or element.get_attribute("data-answer-value")
        or element.get_attribute("aria-label")
        or element.get_attribute("value")
        or element.text.strip()
    )


def _checked(element) -> bool:
    return element.get_attribute("aria-checked") == "true" or bool(element.is_selected())


def _click(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    element.click()


def _wait(driver, timeout, condition, message):
    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.1).until(condition)
    except TimeoutException as exc:
        raise RuntimeError(message) from exc


def _set_value(driver, field, text, timeout, message):
    """Set literal form data without interpreting newlines or WebDriver keys."""
    retained = driver.execute_script(
        """
        const input = arguments[0], value = arguments[1];
        if (input.disabled || input.readOnly) throw new Error('The field is not editable.');
        const prototype = input.tagName === 'TEXTAREA'
            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, value);
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        return input.value;
    """,
        field,
        text,
    )
    if retained != text:
        _wait(driver, timeout, lambda _: field.get_attribute("value") == text, message)


def _rebind_box(driver, question, current):
    """Find a replacement container by stable identity after a DOM render."""
    if not hasattr(driver, "find_elements"):
        return current
    candidates = []
    for candidate in _question_boxes(driver):
        names = {
            re.sub(r"_(sentinel|year|month|day|hour|minute|second)$", "", e.get_attribute("name") or "")
            for e in candidate.find_elements(By.CSS_SELECTOR, '[name^="entry."]')
        }
        if question.entry_id and "entry." + question.entry_id in names:
            candidates.append(candidate)
        elif question.item_id and candidate.get_attribute("data-item-id") == question.item_id:
            candidates.append(candidate)
        elif (
            not question.entry_id
            and not question.item_id
            and _heading(candidate) == _normalized(question.text)
        ):
            candidates.append(candidate)
    if len(candidates) > 1:
        raise RuntimeError(f"Ambiguous replacement controls for {question.text!r}; inspect the form again.")
    return candidates[0] if candidates else None


def _locate_controls(driver, question, box, selector, timeout):
    def ready(_):
        nonlocal box
        try:
            elements = _visible(box.find_elements(By.CSS_SELECTOR, selector))
            if elements:
                return box, elements
        except StaleElementReferenceException:
            pass
        replacement = _rebind_box(driver, question, box)
        if replacement is not None:
            box = replacement
        return False

    return ready(driver) or _wait(
        driver, timeout, ready, f"No ready answer controls for {question.text!r}; the form may have changed."
    )


def _clear_choice(driver, box, question, controls, timeout):
    """Clear an existing optional answer through its supported UI control."""
    if all(not _checked(control) for control in controls):
        return
    buttons = _visible(box.find_elements(By.CSS_SELECTOR, "[role='button'], button"))
    clear = [
        button
        for button in buttons
        if "Clear selection"
        in {_normalized(button.text), _normalized(button.get_attribute("aria-label") or "")}
    ]
    if len(clear) != 1:
        raise RuntimeError(
            f"Cannot clear the existing selection for optional question {question.text!r}; reopen a fresh form before filling a blank answer."
        )
    _click(driver, clear[0])
    _wait(
        driver,
        timeout,
        lambda _: all(not _checked(control) for control in controls),
        f"The browser did not clear the selection for {question.text!r}.",
    )


def _fill_date(driver, question, box, value, timeout):
    box, fields = _locate_controls(driver, question, box, "input:not([type='hidden'])", timeout)
    native = [field for field in fields if field.get_attribute("type") == "date"]
    parts = {}
    for field in fields:
        label = (field.get_attribute("aria-label") or "").lower()
        name = field.get_attribute("name") or ""
        for part in ("year", "month", "day", "hour", "minute"):
            if label == part or name.endswith("_" + part):
                if part in parts:
                    raise RuntimeError(f"Ambiguous date controls for {question.text!r}.")
                parts[part] = field
    required = {"hour", "minute"} if question.date_include_time else set()
    if len(native) != 1:
        required |= {"month", "day"}
        if question.date_include_year:
            required.add("year")
    if len(native) > 1 or not required.issubset(parts):
        raise RuntimeError(
            f"Could not identify the date controls for {question.text!r}; inspect the changed form again."
        )
    parsed = parse_date(question, str(value))[1] if value not in (None, "") else None
    targets = []
    if native:
        targets.append((native[0], parsed.date().isoformat() if parsed else ""))
    targets.extend((parts[part], str(getattr(parsed, part)) if parsed else "") for part in sorted(required))
    for field, text in targets:
        _set_value(
            driver, field, text, timeout, f"The browser did not retain the date for {question.text!r}."
        )
        if text and not driver.execute_script("return arguments[0].validity.valid;", field):
            raise RuntimeError(f"The date is outside the form's allowed range for {question.text!r}.")


def _fill_question(driver, question: QuestionConfig, box, value, timeout):
    for attempt in range(3):
        try:
            return _fill_question_once(driver, question, box, value, timeout)
        except StaleElementReferenceException as exc:
            if attempt == 2:
                raise RuntimeError(
                    f"The controls for {question.text!r} repeatedly changed while filling."
                ) from exc
            box = _wait(
                driver,
                timeout,
                lambda _, previous=box: _rebind_box(driver, question, previous),
                f"The controls for {question.text!r} were replaced and could not be found again.",
            )


def _fill_question_once(driver, question: QuestionConfig, box, value, timeout):
    if question.qtype not in _SUPPORTED:
        raise RuntimeError(f"Unsupported question type {question.qtype!r}: {question.text}")
    if question.qtype == "date":
        _fill_date(driver, question, box, value, timeout)
        return
    if question.qtype in {"text", "textarea"}:
        box, fields = _locate_controls(driver, question, box, "input:not([type='hidden']), textarea", timeout)
        if len(fields) != 1:
            raise RuntimeError(
                f"Expected one text field for {question.text!r}; inspect the changed form again."
            )
        text = "" if value is None else str(value)
        if question.qtype == "text" and ("\n" in text or "\r" in text):
            raise RuntimeError(
                f"Single-line answer for {question.text!r} contains a line break; use one line or a paragraph question."
            )
        _set_value(
            driver, fields[0], text, timeout, f"The browser did not retain the text for {question.text!r}."
        )
        return
    if question.qtype == "dropdown":
        box, dropdowns = _locate_controls(
            driver, question, box, "select, [role='listbox'], [role='combobox']", timeout
        )
        native = [element for element in dropdowns if element.tag_name.lower() == "select"]
        if native:
            from selenium.webdriver.support.ui import Select

            if value in (None, ""):
                blank = [
                    option
                    for option in native[0].find_elements(By.TAG_NAME, "option")
                    if (option.get_attribute("value") or "") == ""
                ]
                if len(blank) != 1:
                    raise RuntimeError(
                        f"Cannot clear the dropdown for optional question {question.text!r}; no unambiguous empty option exists."
                    )
                Select(native[0]).select_by_value("")
                if native[0].get_attribute("value") != "":
                    raise RuntimeError(f"Dropdown blank was not retained for {question.text!r}.")
                return
            Select(native[0]).select_by_visible_text(str(value))
            if Select(native[0]).first_selected_option.text.strip() != str(value):
                raise RuntimeError(f"Dropdown selection was not retained for {question.text!r}.")
            return
        controls = dropdowns
        if len(controls) != 1:
            raise RuntimeError(f"Could not locate the dropdown for {question.text!r}.")
        if value in (None, ""):
            chosen = {
                controls[0].get_attribute(attribute) for attribute in ("data-value", "aria-label", "value")
            }
            selected_options = [
                el
                for el in box.find_elements(By.CSS_SELECTOR, "[role='option']")
                if el.get_attribute("aria-selected") == "true" and _control_value(el) in question.options
            ]
            if chosen.intersection(question.options) or selected_options:
                raise RuntimeError(
                    f"Cannot clear the existing dropdown selection for optional question {question.text!r}; reopen a fresh form before filling a blank answer."
                )
            if _normalized(controls[0].text) in {_normalized(option) for option in question.options}:
                raise RuntimeError(f"The dropdown for {question.text!r} is not blank; reopen a fresh form.")
            return
        _click(driver, controls[0])
        # Google renders its option popup inside this question. Never search
        # all questions by text, which would mix repeated option labels.
        option = _wait(
            driver,
            timeout,
            lambda _: next(
                (
                    el
                    for el in _visible(box.find_elements(By.CSS_SELECTOR, "[role='option']"))
                    if _control_value(el) == str(value)
                ),
                None,
            ),
            f"Dropdown option {value!r} was not found for {question.text!r}.",
        )
        _click(driver, option)
        _wait(
            driver,
            timeout,
            lambda _: (
                controls[0].get_attribute("data-value") == str(value)
                or controls[0].get_attribute("aria-label") == str(value)
                or any(
                    el.get_attribute("aria-selected") == "true" and _control_value(el) == str(value)
                    for el in box.find_elements(By.CSS_SELECTOR, "[role='option']")
                )
                or (
                    controls[0].get_attribute("aria-expanded") == "false"
                    and _normalized(controls[0].text) == _normalized(str(value))
                )
            ),
            f"Dropdown selection was not retained for {question.text!r}.",
        )
        return
    role = "checkbox" if question.qtype == "checkbox" else "radio"
    box, controls = _locate_controls(driver, question, box, f"[role='{role}'], input[type='{role}']", timeout)
    selected = (
        set(value or []) if role == "checkbox" else ({str(value)} if value not in (None, "") else set())
    )
    if role == "checkbox" and not isinstance(value, (list, tuple, type(None))):
        raise RuntimeError(f"Checkbox answers must be arrays for {question.text!r}.")

    def identify(elements):
        by_value = {}
        for control in elements:
            label = _control_value(control)
            if label in by_value:
                raise RuntimeError(f"Option {label!r} is ambiguous in {question.text!r}.")
            by_value[label] = control
        missing = selected - set(by_value)
        if missing:
            raise RuntimeError(
                f"The form no longer has options {sorted(missing)!r} for {question.text!r}; inspect again."
            )
        return by_value

    by_value = identify(controls)  # Validate every option before the first click.
    if role == "radio" and not selected:
        _clear_choice(driver, box, question, controls, timeout)
        return
    attempts, last_attempt = 0, 0.0

    def retain(_):
        nonlocal attempts, last_attempt, by_value, box
        try:
            elements = _visible(box.find_elements(By.CSS_SELECTOR, f"[role='{role}'], input[type='{role}']"))
            if not elements:
                replacement = _rebind_box(driver, question, box)
                if replacement is not None:
                    box = replacement
                return False
            labels = {_control_value(control) for control in elements}
            if not selected.issubset(labels):
                return False
            by_value = identify(elements)
            if all(_checked(control) == (label in selected) for label, control in by_value.items()):
                return True
            now = time.monotonic()
            if attempts < 3 and now - last_attempt >= 0.5:
                attempts += 1
                last_attempt = now
                for label, control in by_value.items():
                    wanted = label in selected
                    # A page may paint controls before its event handlers are ready.
                    # Retry only unretained choices, checking checkbox state first.
                    if (role == "checkbox" and _checked(control) != wanted) or (
                        role == "radio" and wanted and not _checked(control)
                    ):
                        _click(driver, control)
            return all(_checked(control) == (label in selected) for label, control in by_value.items())
        except StaleElementReferenceException:
            replacement = _rebind_box(driver, question, box)
            if replacement is not None:
                box = replacement
            return False
        except (ElementClickInterceptedException, ElementNotInteractableException):
            return False

    _wait(
        driver,
        timeout,
        retain,
        f"The browser did not retain the choices for {question.text!r} after bounded selection attempts.",
    )


def _button(driver, label):
    matches = []
    for element in _visible(
        driver.find_elements(By.CSS_SELECTOR, "[role='button'], button, input[type='submit']")
    ):
        values = {
            _normalized(element.text),
            _normalized(element.get_attribute("aria-label") or ""),
            _normalized(element.get_attribute("value") or ""),
        }
        if label in values and element.is_enabled():
            matches.append(element)
    if len(matches) > 1:
        raise RuntimeError(f"Found multiple {label} buttons; refusing ambiguous navigation.")
    return matches[0] if matches else None


def _validation_errors(driver):
    errors = []
    for element in _visible(
        driver.find_elements(By.CSS_SELECTOR, "[role='alert'], .RHiWt, [aria-invalid='true']")
    ):
        text = _normalized(element.text)
        if text and text not in errors:
            errors.append(text)
        elif not text and element.get_attribute("aria-invalid") == "true":
            errors.append("A required or validated control was rejected.")
    return errors


def _check_interstitial(driver):
    url = urlparse(driver.current_url)
    if url.hostname == "accounts.google.com":
        raise RuntimeError(
            "The form redirected to Google sign-in. Public forms without sign-in are supported."
        )
    body = driver.find_elements(By.TAG_NAME, "body")
    text = _normalized(body[0].text).lower() if body else ""
    for phrase in (
        "you need permission",
        "no longer accepting responses",
        "sign in to continue",
        "something went wrong",
        "try again later",
    ):
        if phrase in text and not _question_boxes(driver):
            raise RuntimeError(f"Google Forms cannot be filled: {phrase}.")


def _positive_confirmation(driver, confirmation_text: str = "", expected_url: str = "") -> bool:
    """A redirect alone says nothing about whether Google accepted a response."""
    current = urlparse(driver.current_url)
    expected = urlparse(expected_url)
    if expected_url and (current.scheme, current.netloc) != (expected.scheme, expected.netloc):
        return False
    path = current.path.rstrip("/")
    if expected_url:
        expected_path = urlparse(expected_url).path.rstrip("/").rsplit("/", 1)[0] + "/formResponse"
        if path != expected_path:
            return False
    if not path.endswith("/formResponse") or _validation_errors(driver) or _question_boxes(driver):
        return False
    markers = _visible(
        driver.find_elements(
            By.CSS_SELECTOR,
            ".vHW8K, .freebirdFormviewerViewResponseConfirmationMessage, [data-gformbot-confirmation]",
        )
    )
    if confirmation_text:
        expected = _normalized(confirmation_text)
        return any(_normalized(element.text) == expected for element in markers)
    if any(element.text.strip() for element in markers):
        return True
    body = driver.find_elements(By.TAG_NAME, "body")
    return bool(body and "your response has been recorded" in _normalized(body[0].text).lower())


def _empty_section_marker(driver, form: RunConfig, section: int):
    """Recognize an inspected info-only section by its exact visible heading."""
    if section >= len(form.sections) or any((q.page - 1) == section for q in form.questions):
        return None
    expected = {_normalized(form.sections[section])}
    if section == 0:
        expected.add(_normalized(form.form_title))
    headings = _visible(
        driver.find_elements(
            By.CSS_SELECTOR,
            ".O8VmIc, .F9yp7e, .freebirdFormviewerViewHeaderTitle, [role='heading'], h1, h2, h3",
        )
    )
    return next((element for element in headings if _normalized(element.text) in expected), None)


def execute_response(
    driver,
    form: RunConfig,
    answers: dict,
    submit: bool = False,
    timeout: float = 15,
    *,
    before_submit: Callable[[], None] | None = None,
) -> dict:
    """Fill every known page; return before Submit unless submit=True."""
    _require_supported(form)
    driver._gformbot_submission_clicked = False
    driver.get(form.url)
    _wait(driver, timeout, lambda d: d.find_elements(By.TAG_NAME, "body"), "The form page did not load.")
    seen = set()
    signatures = set()
    filled = []
    page_cap = max(2, len(form.sections) + 2)
    for page_index in range(page_cap):
        _check_interstitial(driver)
        page = _page_questions(driver, form)
        signature = tuple(question.index for question, _box in page)
        if page and {(q.page - 1) for q, _box in page} != {page_index}:
            raise RuntimeError(
                "The live sections do not follow the inspected order. Branching or a changed form cannot be filled reliably."
            )
        empty_marker = None
        if not signature:
            empty_marker = _empty_section_marker(driver, form, page_index)
            if empty_marker is None:
                raise RuntimeError(
                    "No recognized answer controls are visible. The form may require sign-in or have changed."
                )
            signature = (f"section:{page_index}",)
        if signature in signatures or any(key in seen for key in signature):
            raise RuntimeError(
                "Next returned to an already filled page. Check the form's validation or branching settings."
            )
        signatures.add(signature)
        for question, box in page:
            if question.index not in answers:
                raise RuntimeError(f"The frozen response is missing {question.text!r} ({question.index}).")
            _fill_question(driver, question, box, answers[question.index], timeout)
            filled.append(question.index)
            seen.add(question.index)
        errors = _validation_errors(driver)
        if errors:
            raise RuntimeError("Form validation failed: " + "; ".join(errors))
        submit_button = _button(driver, "Submit")
        if submit_button is not None:
            missing = {q.index for q in form.questions} - seen
            if missing:
                raise RuntimeError(
                    f"Submit appeared before all planned questions were reached: {sorted(missing)}. Re-inspect the form; branching is not supported."
                )
            result = {"status": "filled", "filled_questions": filled, "final_url": driver.current_url}
            if not submit:
                return result
            if before_submit is not None:
                before_submit()
            driver._gformbot_submission_clicked = True
            try:
                _click(driver, submit_button)
                _wait(
                    driver,
                    timeout,
                    lambda d: (
                        _positive_confirmation(d, form.confirmation_text, form.url) or _validation_errors(d)
                    ),
                    "Submit was attempted but acceptance was not confirmed. Check the form's Responses tab before retrying.",
                )
                errors = _validation_errors(driver)
                if errors:
                    raise SubmissionRejectedError("Form rejected the response: " + "; ".join(errors))
                if not _positive_confirmation(driver, form.confirmation_text, form.url):
                    raise SubmissionUncertainError(
                        "Submit was attempted but no positive confirmation appeared. Check the form before retrying."
                    )
            except KeyboardInterrupt:
                raise
            except SubmissionRejectedError:
                raise
            except RuntimeError as exc:
                raise SubmissionUncertainError(str(exc)) from exc
            except Exception as exc:
                raise SubmissionUncertainError(
                    "Submit was attempted but the browser failed before confirmation. Check the form before retrying."
                ) from exc
            return {"status": "confirmed", "filled_questions": filled, "final_url": driver.current_url}
        next_button = _button(driver, "Next")
        if next_button is None:
            raise RuntimeError(
                "Neither Next nor Submit is visible. Use the public form link; English navigation is currently required."
            )
        _click(driver, next_button)

        def advanced(d, signature=signature, page_index=page_index):
            if _validation_errors(d):
                return True
            try:
                new_signature = tuple(q.index for q, _box in _page_questions(d, form))
                if new_signature:
                    return new_signature != signature
                marker = _empty_section_marker(d, form, page_index + 1)
                return marker is not None and (f"section:{page_index + 1}",) != signature
            except StaleElementReferenceException:
                return False

        _wait(
            driver,
            timeout,
            advanced,
            "Next did not reach a new page. Check required answers and form validation.",
        )
        errors = _validation_errors(driver)
        if errors:
            raise RuntimeError("Next was blocked by form validation: " + "; ".join(errors))
    raise RuntimeError("The form exceeded its inspected page count. Inspect it again before running.")


def submit_response(
    config: RunConfig,
    answer: dict[int, object],
    submit: bool = True,
    driver=None,
    *,
    before_submit: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    """Run one response; a supplied private driver can be reused for a batch."""
    from .browser import get_driver

    owns_driver = driver is None
    if owns_driver:
        driver = get_driver(browser=config.browser, headless=config.headless)
    try:
        result = execute_response(driver, config, answer, submit=submit, before_submit=before_submit)
        return True, "submitted" if result["status"] == "confirmed" else "filled"
    finally:
        if owns_driver:
            try:
                driver.quit()
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning("Could not close the private browser: %s", exc)
