"""
Google Form inspector.

Opens a form URL and scrapes:
- All question texts
- All answer options (radio, checkbox, dropdown)
- Which questions are multi-select
- Which questions are text fields
- Page boundaries (multi-page forms)

Returns a FormSchema that the filler, config generator, and wizard all use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


@dataclass
class QuestionSchema:
    index: int                          # 1-based
    text: str                           # Full question label
    qtype: str                          # "radio" | "checkbox" | "text" | "textarea" | "dropdown" | "unknown"
    required: bool = False
    options: list[str] = field(default_factory=list)
    page: int = 1


@dataclass
class FormSchema:
    url: str
    title: str
    questions: list[QuestionSchema] = field(default_factory=list)
    pages: int = 1

    def get(self, index: int) -> Optional[QuestionSchema]:
        for q in self.questions:
            if q.index == index:
                return q
        return None


def inspect_form(url: str, browser: str = "auto", headless: bool = True,
                 status_cb=None) -> FormSchema:
    """
    Scrape a Google Form and return a FormSchema.

    Parameters
    ----------
    url       : Google Form viewform URL
    browser   : browser name passed to get_driver()
    headless  : run headless (recommended for inspection)
    status_cb : optional callable(message: str) for progress reporting
    """
    from gformbot.browser import get_driver

    def _status(msg: str):
        if status_cb:
            status_cb(msg)

    _status("Launching browser to inspect form…")
    driver = get_driver(browser=browser, headless=headless)

    schema = FormSchema(url=url, title="")

    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(1.5)

        # Title
        try:
            schema.title = driver.find_element(
                By.CSS_SELECTOR, "[data-params] .e8F0yb, .freebirdFormviewerViewHeaderTitle"
            ).text.strip()
        except Exception:
            try:
                schema.title = driver.title.replace(" - Google Forms", "").strip()
            except Exception:
                schema.title = "Google Form"

        _status(f"Detected form: \"{schema.title}\"")

        page_num = 1
        question_index = 1

        MAX_PAGES = 200  # safety net — no real form has more than this

        while page_num <= MAX_PAGES:
            _status(f"Scraping page {page_num}…")
            questions_on_page = _scrape_page(driver, page_num, question_index)
            schema.questions.extend(questions_on_page)
            question_index += len(questions_on_page)

            # A Submit button means this is the last page — stop here
            if _find_button(driver, "Submit") is not None:
                break

            # No Next button either — also the end
            next_btn = _find_button(driver, "Next")
            if next_btn is None:
                break

            # Click Next, wait for the page to change
            prev_url = driver.current_url
            next_btn.click()
            time.sleep(1.5)
            page_num += 1
            schema.pages = page_num

            # If the URL didn't change at all, we're stuck — stop
            if driver.current_url == prev_url:
                break

        schema.pages = page_num
        _status(f"Inspection complete — {len(schema.questions)} questions across {schema.pages} page(s).")
        return schema

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Page scraping helpers
# ---------------------------------------------------------------------------

def _scrape_page(driver, page_num: int, start_index: int) -> list[QuestionSchema]:
    questions = []

    # Each question lives in a [role=listitem] container
    items = driver.find_elements(By.XPATH, "//*[@role='listitem']")

    for item in items:
        q = _parse_question(driver, item, start_index + len(questions), page_num)
        if q is not None:
            questions.append(q)

    return questions


def _parse_question(driver, item, index: int, page: int) -> Optional[QuestionSchema]:
    # Find the question label text
    label_text = ""
    for sel in [
        ".freebirdFormviewerComponentsQuestionBaseTitle",
        "[data-params] .M7eMe",
        ".M7eMe",
    ]:
        try:
            el = item.find_element(By.CSS_SELECTOR, sel)
            label_text = el.text.strip()
            if label_text:
                break
        except Exception:
            pass

    if not label_text:
        return None  # Not a question container

    # Check required
    required = False
    try:
        item.find_element(By.XPATH, ".//*[contains(@aria-label,'Required') or contains(text(),'*')]")
        required = True
    except Exception:
        pass

    # Detect type & options
    radios = item.find_elements(By.XPATH, ".//*[@role='radio']")
    checkboxes = item.find_elements(By.XPATH, ".//*[@role='checkbox']")
    dropdowns = item.find_elements(By.XPATH, ".//*[@role='option']")
    textareas = item.find_elements(By.XPATH, ".//textarea")
    text_inputs = item.find_elements(By.XPATH, ".//input[not(@type='hidden')]")

    if checkboxes:
        qtype = "checkbox"
        options = _extract_option_labels(checkboxes)
    elif radios:
        qtype = "radio"
        options = _extract_option_labels(radios)
    elif dropdowns:
        qtype = "dropdown"
        options = [el.text.strip() for el in dropdowns if el.text.strip()]
    elif textareas:
        qtype = "textarea"
        options = []
    elif text_inputs:
        qtype = "text"
        options = []
    else:
        qtype = "unknown"
        options = []

    return QuestionSchema(
        index=index,
        text=label_text,
        qtype=qtype,
        required=required,
        options=options,
        page=page,
    )


def _extract_option_labels(elements) -> list[str]:
    labels = []
    for el in elements:
        label = el.get_attribute("aria-label") or ""
        if not label:
            try:
                label = el.find_element(By.XPATH, "./ancestor::label[1]").text.strip()
            except Exception:
                label = el.text.strip()
        if label:
            labels.append(label.strip())
    return labels


def _find_button(driver, label: str):
    from selenium.common.exceptions import NoSuchElementException
    xpaths = [
        f"//*[@role='button'][.//*[normalize-space(text())='{label}']]",
        f"//*[@role='button' and normalize-space(.)='{label}']",
        f"//span[normalize-space(text())='{label}']/ancestor::*[@role='button'][1]",
    ]
    for xp in xpaths:
        try:
            return driver.find_element(By.XPATH, xp)
        except NoSuchElementException:
            pass
    return None
