"""Fill exact prepared responses with verified navigation and confirmation."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import urlparse

from .config import QuestionConfig, RunConfig
from .followups import observe_page
from .routing import SUPPORTED_TYPES, Routing
from .selenium_runtime import (
    By,
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverWait,
)
from .validation import parse_date, parse_time

_SUPPORTED = SUPPORTED_TYPES


class SubmissionUncertainError(RuntimeError):
    """Submit was clicked, but acceptance could not be confirmed."""


class SubmissionRejectedError(RuntimeError):
    """The form explicitly rejected an attempted submission."""


def _require_supported(config: RunConfig, active=None, routing=None):
    try:
        (routing or Routing(config)).require_supported(active)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not config.questions:
        raise RuntimeError("No answerable questions are configured.")
    unknown = [
        q.text
        for q in config.questions
        if q.qtype not in _SUPPORTED and (active is None or q.index in active)
    ]
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
            name = "emailAddress" if question.entry_id == "emailAddress" else "entry." + question.entry_id
            by_entry.setdefault(name, []).append(question)
        if question.item_id:
            by_item.setdefault(question.item_id, []).append(question)
        by_title.setdefault(_normalized(question.text), []).append(question)
    matches, matched_indices = [], set()
    for box in _question_boxes(driver):
        if getattr(form, "email_collection", "unknown") == "verified":
            consent = _verified_email_control(box)
            if consent is not None:
                if not _checked(consent):
                    raise ValueError("In the browser, check the email consent box before continuing.")
                continue
        names = [
            el.get_attribute("name") or ""
            for el in box.find_elements(By.CSS_SELECTOR, '[name^="entry."], [name="emailAddress"]')
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
        if unique and all(q.grid_title for q in unique.values()):
            rows = list(unique.values())
            if len({q.item_id for q in rows}) != 1:
                raise RuntimeError("Ambiguous live grid identity; inspect the form again.")
            expected = [q for q in form.questions if q.item_id == rows[0].item_id]
            for question in expected:
                if question.index in matched_indices:
                    raise RuntimeError(f"The live page repeats {question.text!r}.")
                matches.append((question, _grid_row(box, question)))
                matched_indices.add(question.index)
            continue
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


def _grid_row(box, question):
    """Bind a row by entry identity inside its parent, never by display order."""
    fields = box.find_elements(
        By.CSS_SELECTOR, f'[name="entry.{question.entry_id}"], [name="entry.{question.entry_id}_sentinel"]'
    )
    rows = {}
    for field in fields:
        for row in field.find_elements(
            By.XPATH, "./ancestor::*[@role='radiogroup' or @role='group' or @role='row'][1]"
        ):
            rows[row.id] = row
    if len(rows) == 1:
        return next(iter(rows.values()))
    # Google may keep response sentinels outside the visual row. Its row label
    # is then the stable identity within the already identified parent item.
    rows = {}
    for row in box.find_elements(By.CSS_SELECTOR, '[role="radiogroup"], [role="group"], [role="row"]'):
        label = row.get_attribute("aria-label") or ""
        if _normalized(label) == _normalized(question.grid_row):
            rows[row.id] = row
    if len(rows) != 1:
        raise RuntimeError(f"Unknown or ambiguous grid row {question.grid_row!r}; inspect the form again.")
    return next(iter(rows.values()))


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


def _verified_email_control(box):
    """Recognize account consent without confusing a normal checkbox question."""
    if box.find_elements(By.CSS_SELECTOR, '[name^="entry."]'):
        return None
    controls = _visible(box.find_elements(By.CSS_SELECTOR, "[role='checkbox'], input[type='checkbox']"))
    if len(controls) != 1:
        return None
    control = controls[0]

    def matches(labels):
        return any(
            re.fullmatch(
                r"Record\s+\S+@\S+\s+as the email to be included with my response\.?", _normalized(label)
            )
            for label in labels
        )

    if matches((control.get_attribute("aria-label") or "", control.text)):
        return control
    labels = []
    references = (control.get_attribute("aria-labelledby") or "").split()
    if references:
        targets = [box.find_elements(By.ID, identity) for identity in references]
        if all(len(elements) == 1 for elements in targets):
            labels.append(" ".join(elements[0].text for elements in targets))
    identity = control.get_attribute("id")
    for label in box.find_elements(By.TAG_NAME, "label"):
        if (identity and label.get_attribute("for") == identity) or control in label.find_elements(
            By.CSS_SELECTOR, "input[type='checkbox']"
        ):
            labels.append(label.text)
    if matches(labels):
        return control
    return None


def _verify_email_consent(driver, form, confirm_email=None, before_review=None):
    if getattr(form, "email_collection", "unknown") != "verified":
        return False
    controls = [
        control for box in _question_boxes(driver) if (control := _verified_email_control(box)) is not None
    ]
    if len(controls) != 1:
        raise ValueError(
            "The verified email consent control was not recognized. Check the form in the browser."
        )
    if before_review is not None:
        before_review()
    if not _checked(controls[0]) and confirm_email is not None:
        confirm_email()
        _check_interstitial(driver)
    if not _checked(controls[0]):
        raise ValueError("In the browser, check the email consent box before continuing.")
    return True


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
            for e in candidate.find_elements(By.CSS_SELECTOR, '[name^="entry."], [name="emailAddress"]')
        }
        expected_name = (
            "emailAddress" if question.entry_id == "emailAddress" else "entry." + question.entry_id
        )
        if question.entry_id and expected_name in names:
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
    return (
        _grid_row(candidates[0], question)
        if candidates and question.grid_title
        else candidates[0]
        if candidates
        else None
    )


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
    scoped = box.find_elements(By.CSS_SELECTOR, '[jsname="YlCLKb"] [role="button"], [jsname="YlCLKb"] button')
    clear = [
        button
        for button in buttons
        if button in scoped
        or "Clear selection"
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


def _fill_time(driver, question, box, value, timeout):
    canonical = parse_time(question, value)
    box, fields = _locate_controls(driver, question, box, "input:not([type='hidden'])", timeout)
    native = [field for field in fields if field.get_attribute("type") == "time"]
    if len(native) == 1 and question.qtype == "time":
        targets = [(native[0], canonical)]
    else:
        parts = {}
        required = ("hour", "minute", "second") if question.qtype == "duration" else ("hour", "minute")
        for field in fields:
            label, name = (field.get_attribute("aria-label") or "").lower(), field.get_attribute("name") or ""
            for part in required:
                if label == part or name.endswith("_" + part):
                    if part in parts:
                        raise RuntimeError(f"Ambiguous time controls for {question.text!r}.")
                    parts[part] = field
        if native or set(parts) != set(required):
            raise RuntimeError(f"Could not identify the time controls for {question.text!r}.")
        values = canonical.split(":") if canonical else [""] * len(required)
        targets = [
            (parts[part], str(int(text)) if text else "") for part, text in zip(required, values, strict=True)
        ]
    for field, text in targets:
        _set_value(
            driver, field, text, timeout, f"The browser did not retain the time for {question.text!r}."
        )
        if text and not driver.execute_script("return arguments[0].validity.valid;", field):
            raise RuntimeError(f"The time is outside the form's allowed range for {question.text!r}.")


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
    if question.allow_other:
        from dataclasses import replace

        from .answers import other_answer

        structured = other_answer(question, value) if isinstance(value, dict) else None
        sentinel = "__other_option__"
        if sentinel in question.options:
            raise RuntimeError(f"Ambiguous Other identity for {question.text!r}.")
        fields = box.find_elements(
            By.CSS_SELECTOR,
            f'input[name="entry.{question.entry_id}.other_option_response"], input[type="text"]',
        )
        fields = _visible(fields)
        if len(fields) != 1:
            raise RuntimeError(f"Could not identify the Other text field for {question.text!r}.")
        adapted = replace(question, allow_other=False, options=[*question.options, sentinel])
        choices = [*structured["choices"], sentinel] if structured else value
        if structured and question.qtype == "radio":
            choices = sentinel
        if not structured:
            _set_value(
                driver,
                fields[0],
                "",
                timeout,
                f"The browser did not clear the Other text for {question.text!r}.",
            )
        _fill_question_once(driver, adapted, box, choices, timeout)
        if structured:
            _set_value(
                driver,
                fields[0],
                structured["other"],
                timeout,
                f"The browser did not retain the Other text for {question.text!r}.",
            )
            # Typing custom text can update the choice control; verify both.
            _fill_question_once(driver, adapted, box, choices, timeout)
        return
    if question.qtype not in _SUPPORTED:
        raise RuntimeError(f"Unsupported question type {question.qtype!r}: {question.text}")
    if question.qtype == "date":
        _fill_date(driver, question, box, value, timeout)
        return
    if question.qtype in ("time", "duration"):
        _fill_time(driver, question, box, value, timeout)
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
            from .selenium_runtime import Select

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
    identities = {"OCpkoe": "Next", "M2UYVd": "Submit", "GeGHKb": "Back", "X5DuWc": "Clear form"}
    matches = []
    controls = driver.execute_script("""
        return [...document.querySelectorAll('[role="button"],button,input[type="submit"]')]
          .map(element=>({element, labels:[element.innerText||'',element.getAttribute('aria-label')||'',
             element.getAttribute('value')||''], action:element.getAttribute('jsname')}));
    """)
    for control in controls:
        element = control["element"]
        if not _visible([element]):
            continue
        values = {_normalized(text) for text in control["labels"]}
        identity = identities.get(control["action"])
        if identity and any(text in identities.values() and text != identity for text in values):
            raise RuntimeError(
                "Form control identity conflicts with its label; refusing ambiguous navigation."
            )
        if (identity == label or identity is None and label in values) and element.is_enabled():
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
    from .access import check_browser_access

    check_browser_access(driver)


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
    ids = getattr(form, "section_ids", [])
    if ids and section > 0:
        for element in _visible(driver.find_elements(By.CSS_SELECTOR, "[data-item-id], [data-params]")):
            identity = element.get_attribute("data-item-id")
            if not identity:
                match = re.match(r"\s*%\.@\.\[\s*(\d+)", element.get_attribute("data-params") or "")
                identity = match.group(1) if match else None
            if identity == ids[section]:
                return element
    if section > 0 and any(
        i != section and _normalized(title) in expected and not any(q.page == i + 1 for q in form.questions)
        for i, title in enumerate(form.sections)
    ):
        return None
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
    before_review: Callable[[], None] | None = None,
    confirm_email: Callable[[], None] | None = None,
    observe: Callable | None = None,
) -> dict:
    """Fill and verify the frozen response's route; submit only at its end."""
    routes = getattr(form, "_routing", None) or Routing(form)
    from .generator import validate_answer

    answers = validate_answer(form, answers, routing=routes)
    path = list(routes.walk(answers))
    planned = {q.index for section in path for q in routes.questions[section]}
    _require_supported(form, planned, routes)
    driver._gformbot_submission_clicked = False
    driver.get(form.url)
    _wait(driver, timeout, lambda d: d.find_elements(By.TAG_NAME, "body"), "The form page did not load.")
    seen = set()
    signatures = set()
    filled = []
    for step, section in enumerate(path):
        page_index = section - 1
        next_section = path[step + 1] if step + 1 < len(path) else None
        _check_interstitial(driver)
        if step == 0:
            if _verify_email_consent(driver, form, confirm_email, before_review):
                before_review = None
        page = _page_questions(driver, form)
        signature = tuple(question.index for question, _box in page)
        if {q.index for q, _box in page} != {q.index for q in routes.questions[section]}:
            raise RuntimeError(
                f"The live questions do not match planned section {section}. The route or form changed; submission stopped."
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
        observe_page(observe, driver, section, "section")
        if before_review is not None:
            before_review()
            before_review = None
        signatures.add(signature)
        for question, box in page:
            if question.index not in answers:
                raise RuntimeError(f"The frozen response is missing {question.text!r} ({question.index}).")
            _fill_question(driver, question, box, answers[question.index], timeout)
            filled.append(question.index)
            seen.add(question.index)
        for question, box in page:
            if question.grid_title:
                role = "checkbox" if question.qtype == "checkbox" else "radio"
                selected = {
                    _control_value(control)
                    for control in box.find_elements(
                        By.CSS_SELECTOR, f"[role='{role}'], input[type='{role}']"
                    )
                    if _checked(control)
                }
                expected = answers[question.index]
                if selected != (set(expected) if role == "checkbox" else {expected} if expected else set()):
                    raise RuntimeError(f"The browser did not retain the grid row {question.grid_row!r}.")
        errors = _validation_errors(driver)
        if errors:
            raise RuntimeError("Form validation failed: " + "; ".join(errors))
        label = "Next" if next_section is not None else "Submit"

        def navigation_ready(d, label=label):
            _check_interstitial(d)
            return _button(d, label)

        navigation = _wait(
            driver,
            timeout,
            navigation_ready,
            f"The planned route needs {label} in section {section}, but it did not appear. Re-inspect the form.",
        )
        submit_button = navigation if label == "Submit" else _button(driver, "Submit")
        if submit_button is not None:
            missing = planned - seen
            if missing or next_section is not None:
                raise RuntimeError(
                    "Submit appeared before the planned route was complete. Re-inspect the form before submitting."
                )
            result = {"status": "filled", "filled_questions": filled, "final_url": driver.current_url}
            if not submit:
                return result
            if before_submit is not None:
                before_submit()
            driver._gformbot_submission_clicked = True
            try:
                _click(driver, submit_button)

                def submitted(d):
                    _check_interstitial(d)
                    return _positive_confirmation(d, form.confirmation_text, form.url) or _validation_errors(
                        d
                    )

                _wait(
                    driver,
                    timeout,
                    submitted,
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
            except (RuntimeError, ValueError) as exc:
                raise SubmissionUncertainError(str(exc)) from exc
            except Exception as exc:
                raise SubmissionUncertainError(
                    "Submit was attempted but the browser failed before confirmation. Check the form before retrying."
                ) from exc
            result = {"status": "confirmed", "filled_questions": filled, "final_url": driver.current_url}
            observe_page(observe, driver, 0, "confirmation")
            return result
        _click(driver, navigation)

        def advanced(d, signature=signature, next_section=next_section):
            _check_interstitial(d)
            if _validation_errors(d):
                return True
            try:
                new_signature = tuple(q.index for q, _box in _page_questions(d, form))
                if new_signature:
                    return new_signature != signature
                marker = _empty_section_marker(d, form, next_section - 1)
                return marker is not None and (f"section:{next_section - 1}",) != signature
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
    raise RuntimeError("The planned route did not reach Submit. Inspect the form again before running.")


def submit_response(
    config: RunConfig,
    answer: dict[int, object],
    submit: bool = True,
    driver=None,
    *,
    before_submit: Callable[[], None] | None = None,
    before_review: Callable[[], None] | None = None,
    confirm_email: Callable[[], None] | None = None,
    observe: Callable | None = None,
) -> tuple[bool, str]:
    """Run one response; a supplied private driver can be reused for a batch."""
    from .browser import get_driver

    owns_driver = driver is None
    if owns_driver:
        driver = get_driver(browser=config.browser, headless=config.headless)
    try:
        result = execute_response(
            driver,
            config,
            answer,
            submit=submit,
            before_submit=before_submit,
            before_review=before_review,
            confirm_email=confirm_email,
            observe=observe,
        )
        return True, "submitted" if result["status"] == "confirmed" else "filled"
    finally:
        if owns_driver:
            try:
                driver.quit()
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning("Could not close the private browser: %s", exc)
