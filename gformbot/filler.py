"""
Form filler — drives the browser to fill and submit one response per call.

Designed to be form-agnostic: it works entirely from a RunConfig and the
answer dict produced by the generator. No hard-coded question texts or URLs.
"""

from __future__ import annotations

import random
import time
from typing import Any

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from gformbot.config import QuestionConfig, RunConfig


# ---------------------------------------------------------------------------
# XPath helpers
# ---------------------------------------------------------------------------

def _xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


# ---------------------------------------------------------------------------
# Element finders
# ---------------------------------------------------------------------------

def _question_box(driver, question_text: str, timeout: float = 3):
    q = _xpath_literal(question_text)
    xpaths = [
        f"//*[normalize-space(text())={q}]/ancestor::*[@role='listitem'][1]",
        f"//*[normalize-space(.)={q}]/ancestor::*[@role='listitem'][1]",
    ]
    for xp in xpaths:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, xp))
            )
        except TimeoutException:
            pass
    return None


def _click_option(driver, box, value: str, delay: tuple[float, float]):
    lit = _xpath_literal(value)
    xpaths = [
        f".//*[@role='radio' and @aria-label={lit}]",
        f".//*[@role='checkbox' and @aria-label={lit}]",
        f".//*[normalize-space(text())={lit}]/ancestor::*[@role='radio'][1]",
        f".//*[normalize-space(text())={lit}]/ancestor::*[@role='checkbox'][1]",
        f".//*[normalize-space(.)={lit}]/ancestor::*[@role='radio'][1]",
        f".//*[normalize-space(.)={lit}]/ancestor::*[@role='checkbox'][1]",
        f".//option[normalize-space(text())={lit}]",
    ]
    for xp in xpaths:
        try:
            el = box.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(random.uniform(*delay))
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            return
        except NoSuchElementException:
            pass
    raise RuntimeError(f"Could not find option: {value!r}")


def _fill_text(driver, box, value: str, delay: tuple[float, float]):
    if not value:
        return
    fields = box.find_elements(
        By.XPATH, ".//input[not(@type='hidden')] | .//textarea"
    )
    if not fields:
        raise RuntimeError("Could not find text/textarea field")
    field = fields[0]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
    time.sleep(random.uniform(*delay))
    field.clear()
    field.send_keys(value)


def _click_button(driver, label: str):
    lit = _xpath_literal(label)
    xpaths = [
        f"//*[@role='button'][.//*[normalize-space(text())={lit}]]",
        f"//*[@role='button' and normalize-space(.)={lit}]",
        f"//span[normalize-space(text())={lit}]/ancestor::*[@role='button'][1]",
    ]
    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.3)
            btn.click()
            return
        except TimeoutException:
            pass
    raise RuntimeError(f"Button not found: {label!r}")


# ---------------------------------------------------------------------------
# Fill one question
# ---------------------------------------------------------------------------

def _fill_question(driver, q: QuestionConfig, value: Any,
                   short_delay: tuple[float, float]) -> bool:
    """
    Fill a single question on the current page.
    Returns True if found and filled, False if not found on this page.
    """
    if value is None:
        return False

    box = _question_box(driver, q.text)
    if box is None:
        return False  # Not on this page yet

    if q.qtype == "checkbox":
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item:
                _click_option(driver, box, item, short_delay)
    elif q.qtype in ("text", "textarea"):
        _fill_text(driver, box, str(value), short_delay)
    else:
        # radio / dropdown
        if value:
            _click_option(driver, box, str(value), short_delay)

    time.sleep(random.uniform(*short_delay))
    return True


# ---------------------------------------------------------------------------
# Main filler
# ---------------------------------------------------------------------------

def submit_response(config: RunConfig, answer: dict[int, Any]) -> tuple[bool, str]:
    """
    Open the form, fill every question, and submit.

    Returns
    -------
    (success: bool, message: str)
    """
    from gformbot.browser import get_driver

    short_delay = (config.min_delay, config.max_delay)
    page_delay = (config.page_delay_min, config.page_delay_max)

    driver = get_driver(browser=config.browser, headless=config.headless)
    try:
        driver.get(config.url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(random.uniform(*page_delay))

        # Build ordered list of questions
        questions_by_index = sorted(config.questions, key=lambda q: q.index)
        q_iter = iter(questions_by_index)
        current_q = next(q_iter, None)

        while current_q is not None:
            filled_this_page = 0

            # Fill all questions visible on the current page
            while current_q is not None:
                value = answer.get(current_q.index)
                if _fill_question(driver, current_q, value, short_delay):
                    filled_this_page += 1
                    current_q = next(q_iter, None)
                else:
                    break  # This question isn't on this page yet

            if current_q is None:
                break  # All questions filled, ready to submit

            # Advance to next page
            if filled_this_page == 0:
                raise RuntimeError(
                    f'Question {current_q.index} ("{current_q.text[:50]}") '
                    "not found. The form may have changed."
                )

            _click_button(driver, "Next")
            time.sleep(random.uniform(*page_delay))

            # Verify the next page loaded
            if _question_box(driver, current_q.text, timeout=10) is None:
                raise RuntimeError(
                    f"Did not reach question {current_q.index} after clicking Next. "
                    "Check for validation errors in the browser."
                )

        _click_button(driver, "Submit")

        # Wait for confirmation
        WebDriverWait(driver, 12).until(
            lambda d: (
                "response has been recorded" in d.page_source.lower()
                or "submit another response" in d.page_source.lower()
                or "viewform" not in d.current_url.lower()
            )
        )

        return True, "submitted"

    finally:
        time.sleep(0.4)
        driver.quit()
