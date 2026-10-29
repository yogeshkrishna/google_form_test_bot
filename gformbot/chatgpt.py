"""Use the visible ChatGPT website in one fresh private browser session."""

import math

from .browser import get_driver
from .selenium_runtime import (
    By,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
    WebDriverWait,
)


class ChatGPTError(ValueError):
    """The website could not accept a prompt or finish its response."""


def open_chat(browser):
    driver = get_driver(browser)
    try:
        driver.get("https://chatgpt.com/")
    except BaseException:
        try:
            driver.quit()
        except (WebDriverException, OSError):
            pass  # Preserve the navigation failure when cleanup also fails.
        raise
    return driver


def _editor(driver):
    for element in driver.find_elements(
        By.CSS_SELECTOR, "#prompt-textarea, #mobile-composer-prompt, textarea, [contenteditable='true']"
    ):
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except StaleElementReferenceException:
            continue
    return None


def _messages(driver):
    return driver.find_elements(
        By.CSS_SELECTOR, "[data-message-author-role='assistant'], [data-message-role='assistant']"
    )


def _content(message):
    code = message.find_elements(By.CSS_SELECTOR, "pre code")
    if len(code) == 1:
        return code[0].get_attribute("textContent").strip()
    markdown = message.find_elements(By.CSS_SELECTOR, "[data-assistant-markdown], .markdown")
    return (
        markdown[0].text.strip() if markdown else message.text.strip().removeprefix("ChatGPT said:").strip()
    )


def ask(driver, messages, schema=None, timeout=180, attention=None):
    """Send a prepared prompt once; schema is a legacy argument, not an API contract.

    The caller includes its schema in messages and validates the returned JSON.
    The website itself does not enforce a structured-output schema.
    """
    if (
        not isinstance(messages, (list, tuple))
        or not messages
        or any(not isinstance(m, dict) or not isinstance(m.get("content"), str) for m in messages)
    ):
        raise ChatGPTError("Messages must contain text prompts.")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ChatGPTError("ChatGPT timeout must be a finite positive number.")
    try:
        editor = WebDriverWait(driver, min(20, timeout), poll_frequency=0.2).until(_editor)
    except TimeoutException as exc:
        if attention is None:
            raise ChatGPTError(
                "ChatGPT needs sign-in, a verification step, or its page has changed. Use the interactive wizard to handle this in the browser."
            ) from exc
        attention("Complete any ChatGPT sign-in or verification in the private browser, then press Enter")
        try:
            editor = WebDriverWait(driver, min(30, timeout), poll_frequency=0.2).until(_editor)
        except TimeoutException as retry:
            raise ChatGPTError("ChatGPT still has no ready message editor. Generation stopped.") from retry
    previous = len(_messages(driver))
    prompt = "\n".join(message["content"] for message in messages)
    editor.click()
    # Insert the full prompt once without treating its line breaks as Send keys.
    driver.execute_script(
        """
        const input = arguments[0], value = arguments[1];
        if (input.tagName === 'TEXTAREA') {
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, value);
            input.dispatchEvent(new Event('input', {bubbles: true}));
        } else {
            input.focus();
            const range = document.createRange();
            range.selectNodeContents(input);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            document.execCommand('insertText', false, value);
        }
    """,
        editor,
        prompt,
    )
    retained = (
        editor.get_attribute("value")
        if editor.tag_name.lower() == "textarea"
        else editor.get_attribute("innerText")
    )
    if retained != prompt:
        raise ChatGPTError("ChatGPT did not retain the complete prompt; nothing was sent.")
    try:
        button = WebDriverWait(driver, min(15, timeout), poll_frequency=0.2).until(
            lambda d: next(
                (
                    element
                    for element in d.find_elements(
                        By.CSS_SELECTOR,
                        "button[aria-label='Send message'], button[data-testid='send-button']",
                    )
                    if element.is_displayed() and element.is_enabled()
                ),
                None,
            )
        )
        button.click()
    except TimeoutException as exc:
        raise ChatGPTError("ChatGPT's Send button is unavailable. Check the page or account limits.") from exc

    def finished(d):
        try:
            replies = _messages(d)
            if len(replies) <= previous:
                alerts = d.find_elements(By.CSS_SELECTOR, "[role='alert']")
                error = next((el.text for el in alerts if el.is_displayed() and el.text.strip()), "")
                if error:
                    raise ChatGPTError(f"ChatGPT reported: {error[:400]}")
                return False
            message = replies[-1]
            if message.get_dom_attribute("data-message-streaming") is not None:
                return False
            if any(
                el.is_displayed()
                for el in d.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-testid='stop-button'], button[aria-label='Stop generating'], button[aria-label='Stop streaming']",
                )
            ):
                return False
            content = _content(message)
            return content or False
        except StaleElementReferenceException:
            return False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.3).until(finished)
    except TimeoutException as exc:
        raise ChatGPTError(
            "ChatGPT did not finish its response in time. Check sign-in, usage limits, or the website. No prompt was retried automatically."
        ) from exc
