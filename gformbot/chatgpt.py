"""Use ChatGPT in a private headless session, with manual help before sending."""

import math
import time

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

    def __init__(self, message, *, partial_content=""):
        super().__init__(message)
        self.partial_content = partial_content


def _open_driver(browser, *, headless):
    driver = get_driver(browser, headless=headless)
    try:
        driver.get("https://chatgpt.com/")
    except BaseException:
        try:
            driver.quit()
        except (WebDriverException, OSError):
            pass  # Preserve the navigation failure when cleanup also fails.
        raise
    return driver


class _ChatSession:
    """Keep the caller's session stable if initial sign-in needs a visible browser."""

    def __init__(self, browser):
        self.browser = browser
        self.headless = True
        self.sent = False
        self._driver = _open_driver(browser, headless=True)

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def show(self):
        if self.sent:
            raise ChatGPTError("ChatGPT's session cannot be restarted after sending a prompt.")
        self.quit()
        self._driver = _open_driver(self.browser, headless=False)
        self.headless = False

    def quit(self):
        driver, self._driver = self._driver, None
        if driver is not None:
            driver.quit()


def open_chat(browser):
    return _ChatSession(browser)


def new_chat(driver):
    """Reset context in the same private session before sending a fresh form prompt."""
    try:
        driver.get("https://chatgpt.com/")
        WebDriverWait(driver, 20, poll_frequency=0.2).until(lambda d: not _messages(d))
    except (WebDriverException, OSError) as exc:
        raise ChatGPTError("Could not start a fresh ChatGPT conversation. Generation stopped.") from exc


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


def _verification_page(driver):
    """Require both the challenge title and markup before naming the block."""
    try:
        return driver.title.strip() == "Just a moment..." and bool(
            driver.find_elements(
                By.CSS_SELECTOR,
                "script[src*='/cdn-cgi/challenge-platform'], #challenge-stage, #challenge-running",
            )
        )
    except (AttributeError, WebDriverException, OSError):
        return False


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
        verification = _verification_page(driver)
        reason = "Cloudflare browser verification is blocking ChatGPT. " if verification else ""
        if isinstance(driver, _ChatSession) and driver.headless and driver.sent:
            raise ChatGPTError(
                reason
                + "ChatGPT's headless session is no longer ready. Generation stopped; accepted answers are preserved."
            ) from exc
        if attention is None:
            raise ChatGPTError(
                (reason or "ChatGPT needs sign-in, a verification step, or its page has changed. ")
                + "Use the interactive wizard to open a visible private browser and complete any verification manually, or supply answers with --data."
            ) from exc
        if isinstance(driver, _ChatSession) and driver.headless:
            attention(
                (reason or "ChatGPT needs your attention. ") + "Press Enter to open a visible private browser"
            )
            driver.show()
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
        if isinstance(driver, _ChatSession):
            driver.sent = True  # An uncertain Send must never trigger a session restart.
        button.click()
    except TimeoutException as exc:
        raise ChatGPTError("ChatGPT's Send button is unavailable. Check the page or account limits.") from exc

    latest = ""
    stable_since = 0.0

    def finished(d):
        nonlocal latest, stable_since
        try:
            replies = _messages(d)
            if len(replies) > previous:
                message = replies[-1]
                content = _content(message)
                if content != latest:
                    latest, stable_since = content, time.monotonic()
            alerts = d.find_elements(By.CSS_SELECTOR, "[role='alert']")
            error = next((el.text for el in alerts if el.is_displayed() and el.text.strip()), "")
            if error:
                raise ChatGPTError(f"ChatGPT reported: {error[:400]}", partial_content=latest)
            if len(replies) <= previous:
                return False
            if message.get_dom_attribute("data-message-streaming") is not None:
                stable_since = time.monotonic()
                return False
            if any(
                el.is_displayed()
                for el in d.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-testid='stop-button'], button[aria-label='Stop generating'], button[aria-label='Stop streaming']",
                )
            ):
                stable_since = time.monotonic()
                return False
            # The reply can appear before the site's streaming controls update.
            return content if content and time.monotonic() - stable_since >= 0.6 else False
        except StaleElementReferenceException:
            return False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.3).until(finished)
    except TimeoutException as exc:
        raise ChatGPTError(
            "ChatGPT did not finish its response in time. Check sign-in, usage limits, or the website. No prompt was retried automatically.",
            partial_content=latest,
        ) from exc
