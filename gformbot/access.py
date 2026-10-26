"""Recognize access failures without treating question text as an error."""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_MESSAGES = {
    "login_required": "This form requires sign-in or access permission. Use --sign-in to sign in directly in the browser.",
    "permission_denied": "This account does not have permission to answer the form. Ask the owner for access or use an allowed account.",
    "already_answered": "This account has already answered the form. This does not confirm that the current saved response was submitted.",
    "closed": "This form is closed, unpublished, or unavailable. Ask the owner to enable responses.",
    "temporary_failure": "Google Forms is temporarily unavailable. Check the connection and try again later.",
    "unknown": "No public form schema was found. The page could not be recognized; check the responder link and access in your browser.",
}


class FormAccessError(ValueError):
    """A known access block, separate from invalid answers or accepted responses."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(_MESSAGES[state])


class _AccessPage(HTMLParser):
    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.status_text = []
        self.has_answers = False
        self.has_form = False
        self.confirmed = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else ("", False, False, False)
        question = attrs.get("role") == "listitem"
        hidden = (
            "hidden" in attrs
            or attrs.get("aria-hidden") == "true"
            or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style", ""), re.I)
        )
        ignored = parent[1] or tag in {"script", "style", "template", "noscript"} or hidden
        classes = attrs.get("class", "").split()
        confirmation = (
            "vHW8K" in classes
            or "freebirdFormviewerViewResponseConfirmationMessage" in classes
            or "data-gformbot-confirmation" in attrs
        )
        if confirmation and not ignored:
            self.confirmed = True
        if tag == "form" and not ignored:
            self.has_form = True
        if not ignored and (
            question
            or tag in {"textarea", "select"}
            or (tag == "input" and attrs.get("type", "text") not in {"hidden", "submit", "button"})
            or attrs.get("role") in {"radio", "checkbox", "textbox", "listbox", "combobox"}
        ):
            self.has_answers = True
        status = parent[2] or bool(
            {"freebirdFormviewerViewResponseMessage", "freebirdFormviewerViewResponseStatus"} & set(classes)
        )
        if tag not in self._VOID:
            self.stack.append((tag, bool(ignored), status, parent[3] or question or confirmation))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        parent = self.stack[-1] if self.stack else ("", False, False, False)
        if not parent[1] and not parent[3]:
            self.text.append(data)
            if parent[2]:
                self.status_text.append(data)


_PATTERNS = (
    (
        "already_answered",
        r"you(?:'ve| have) already (?:responded|submitted|answered)|you can only (?:fill out|respond to) this form once",
    ),
    (
        "permission_denied",
        r"you (?:need|do not have|don't have) permission|request access|only .*?within .*?organization|part of the owner's organization|access denied",
    ),
    (
        "closed",
        r"(?:no longer|not|isn't|has stopped) accepting responses|form (?:is|has been) (?:closed|unpublished|deleted)|form is not (?:yet )?published|(?:form|file) (?:you have requested )?does not exist",
    ),
    (
        "login_required",
        r"sign in to (?:continue|fill out|submit|respond)|you must (?:be signed in|sign in)|sign[- ]in (?:is )?required",
    ),
    ("temporary_failure", r"something went wrong|try again later|temporarily unavailable|too many requests"),
)


def classify_access(document: str, url: str = "", status: int | None = None) -> str | None:
    """Return a proven block; absence of metadata alone is not proof of closure."""
    parsed = urlparse(url)
    if parsed.hostname == "accounts.google.com" or status == 401:
        return "login_required"
    if status == 403:
        return "permission_denied"
    if status in {404, 410} or parsed.path.rstrip("/").endswith("/closedform"):
        return "closed"
    if status == 429 or (status is not None and 500 <= status <= 599):
        return "temporary_failure"
    page = _AccessPage()
    page.feed(document)
    if page.confirmed:
        return None
    # Embedded data and live controls normally mean a usable form. Only a
    # dedicated response status can override that, not its title or examples.
    text = page.status_text
    if not text and not page.has_answers and not page.has_form:
        text = page.text
    return _classify_message(" ".join(text))


def _classify_message(text: str) -> str | None:
    normalized = " ".join(text.split()).lower().replace("\u2019", "'")
    for state, pattern in _PATTERNS:
        if re.search(pattern, normalized):
            return state
    return None


def raise_for_access(document: str = "", url: str = "", status: int | None = None, *, unknown=False) -> None:
    state = classify_access(document, url, status)
    if state is not None or unknown:
        raise FormAccessError(state or "unknown")


def check_browser_access(driver) -> None:
    """Check the current session without reading or retaining authentication data."""
    url = driver.current_url
    raise_for_access(url=url)
    execute_script = getattr(driver, "execute_script", None)
    if callable(execute_script):
        message = execute_script(
            """
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            const confirmation = '.vHW8K, .freebirdFormviewerViewResponseConfirmationMessage, [data-gformbot-confirmation]';
            if ([...document.querySelectorAll(confirmation)].some(visible)) return '';
            const statuses = [...document.querySelectorAll('.freebirdFormviewerViewResponseMessage, .freebirdFormviewerViewResponseStatus')];
            const status = statuses.filter(e => visible(e) && !e.closest('[role="listitem"]')).map(e => e.innerText).join(' ');
            if (status) return status;
            const controls = 'form, input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select, [role="radio"], [role="checkbox"], [role="textbox"], [role="listbox"], [role="combobox"]';
            if ([...document.querySelectorAll(controls)].some(visible) || !document.body) return '';
            const parts = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const parent = node.parentElement;
                if (parent && visible(parent) && !parent.closest('script, style, template, noscript, [role="listitem"], [hidden], [aria-hidden="true"]')) parts.push(node.textContent);
            }
            return parts.join(' ');
            """
        )
        if isinstance(message, str):
            state = _classify_message(message)
            if state:
                raise FormAccessError(state)
            return
    document = getattr(driver, "page_source", "")
    if not isinstance(document, str):
        document = ""
    raise_for_access(document, url)
