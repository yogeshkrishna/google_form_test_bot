"""Submit validated public-form profiles over a pooled, non-retrying HTTP connection."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import certifi
import urllib3

from .access import (
    FormAccessError,
    SubmissionRejectedError,
    SubmissionUncertainError,
    raise_for_access,
    require_public_form,
)
from .generator import validate_answer
from .inspector import clean_url, parse_form_html, schema_fingerprint
from .routing import Routing

_LIMIT = 10_000_000
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_CONFIRMATION = {"vHW8K", "freebirdFormviewerViewResponseConfirmationMessage"}


class _Page(HTMLParser):
    """Read only protocol fields, scoped status and actual completion markup."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.hidden, self.confirmations, self.errors = {}, [], []
        self.stack = []
        self.challenge = self.controls = False
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else ("", False, None, None)
        ignored = (
            parent[1]
            or tag in {"script", "style", "template", "noscript"}
            or (
                "hidden" in attrs
                or attrs.get("aria-hidden") == "true"
                or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I)
            )
        )
        classes = set(attrs.get("class", "").split())
        confirmation, error = parent[2:]
        if not ignored:
            if (_CONFIRMATION & classes or "data-gformbot-confirmation" in attrs) and confirmation is None:
                confirmation = len(self.confirmations)
                self.confirmations.append({"text": "", "links": []})
            if attrs.get("role") == "alert" or "RHiWt" in classes or attrs.get("aria-invalid") == "true":
                error = len(self.errors)
                self.errors.append("")
            if tag == "a" and confirmation is not None and attrs.get("href"):
                self.confirmations[confirmation]["links"].append(attrs["href"])
            if tag == "br" and confirmation is not None:
                self.confirmations[confirmation]["text"] += "\n"
            self.controls |= (
                attrs.get("role")
                in {
                    "radio",
                    "checkbox",
                    "textbox",
                    "listbox",
                    "combobox",
                }
                or tag in {"textarea", "select"}
                or (
                    tag == "input" and attrs.get("type", "text").lower() not in {"hidden", "submit", "button"}
                )
            )
            self.challenge |= (
                "g-recaptcha" in classes
                or attrs.get("name") == "g-recaptcha-response"
                or tag == "iframe"
                and "recaptcha" in attrs.get("src", "").lower()
            )
        if tag == "input" and attrs.get("type", "text").lower() == "hidden" and attrs.get("name"):
            name, value = attrs["name"], attrs.get("value", "")
            if name in self.hidden and self.hidden[name] != value:
                raise ValueError("The form contains conflicting hidden fields; inspect it again.")
            self.hidden[name] = value
        if tag not in _VOID:
            self.stack.append((tag, bool(ignored), confirmation, error))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        if not self.stack or self.stack[-1][1]:
            return
        _, _, confirmation, error = self.stack[-1]
        if confirmation is not None:
            self.confirmations[confirmation]["text"] += value
        if error is not None:
            self.errors[error] += value


def _values(question, value):
    """Serialize one normalized answer; repeated keys preserve checkbox labels."""
    name = "entry." + question.entry_id
    if question.entry_id == "emailAddress":
        return [("emailAddress", value)] if value else []
    if not question.entry_id.isdecimal():
        raise ValueError(f"Q{question.index}: HTTP execution requires a Google entry identity.")
    result = []
    if question.qtype in {"radio", "checkbox", "scale", "rating"}:
        result.append((name + "_sentinel", ""))
    if value in (None, "", []):
        return result
    if isinstance(value, dict):
        return (
            result
            + [(name, choice) for choice in value["choices"]]
            + [
                (name, "__other_option__"),
                (name + ".other_option_response", value["other"]),
            ]
        )
    if question.qtype == "date":
        date, _, clock = value.partition(" ")
        parts = ("year", "month", "day") if question.date_include_year else ("month", "day")
        result.extend(
            (name + "_" + part, str(int(number))) for part, number in zip(parts, date.split("-"), strict=True)
        )
        if clock:
            result.extend(
                (name + "_" + part, number)
                for part, number in zip(("hour", "minute"), clock.split(":"), strict=True)
            )
        return result
    if question.qtype in {"time", "duration"}:
        parts = ("hour", "minute", "second") if question.qtype == "duration" else ("hour", "minute")
        return result + [
            (name + "_" + part, number) for part, number in zip(parts, value.split(":"), strict=True)
        ]
    return result + [(name, item) for item in (value if isinstance(value, list) else [value])]


class Client:
    """One form snapshot and reusable connection pool per execution batch."""

    def __init__(self, config):
        self.config = config
        require_public_form(config)
        self.routes = Routing(config)
        self.pool = urllib3.PoolManager(
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            timeout=urllib3.Timeout(connect=5, read=30),
            retries=False,
            headers={"User-Agent": "gformbot (public form submission)"},
        )
        try:
            requested = clean_url(config.url)
            for _ in range(6):
                status, headers, document = self._request("GET", requested)
                raise_for_access(document, requested, status)
                if status in {301, 302, 303, 307, 308} and headers.get("Location"):
                    requested = clean_url(urljoin(requested, headers["Location"]))
                    continue
                break
            else:
                raise ValueError("Too many form redirects; check the responder link.")
            if status != 200:
                raise ValueError(f"Could not read the public form (HTTP {status}).")
            self.url = clean_url(requested, allow_short=False)
            current = parse_form_html(document, self.url)
            require_public_form(current)
            if schema_fingerprint(current) != schema_fingerprint(config):
                raise ValueError("The form has changed since inspection. Prepare a new response plan.")
            if getattr(config, "_instructions_known", True) and current.instructions != config.instructions:
                raise ValueError("The form instructions have changed. Prepare a new response plan.")
            page = _Page(document)
            if page.challenge:
                raise ValueError(
                    "Google requires verification. Complete the form manually; no response was sent."
                )
            self.token = page.hidden.get("fbzx")
            if not self.token or page.hidden.get("pageHistory") != "0":
                raise ValueError("Google's submission fields are unrecognized; no response was sent.")
            self.endpoint = self.url.rsplit("/", 1)[0] + "/formResponse"
        except BaseException:
            self.close()
            raise

    def _request(self, method, url, body=None):
        response = self.pool.request(
            method,
            url,
            body=body,
            redirect=False,
            retries=False,
            headers={
                "User-Agent": "gformbot (public form submission)",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            if body is not None
            else None,
            preload_content=False,
        )
        try:
            data = response.read(_LIMIT + 1)
            if len(data) > _LIMIT:
                raise ValueError("Google's response exceeded the safe read limit.")
            return response.status, response.headers, data.decode("utf-8-sig")
        except BaseException:
            response.close()
            raise
        finally:
            response.release_conn()

    def _prepare(self, answer):
        answer = validate_answer(self.config, answer, routing=self.routes)
        pages = list(self.routes.walk(answer))
        fields = [
            ("fvv", "1"),
            ("fbzx", self.token),
            ("partialResponse", json.dumps([None, None, self.token], separators=(",", ":"))),
            ("pageHistory", ",".join(str(page - 1) for page in pages)),
            ("submissionTimestamp", "-1"),
        ]
        for page in pages:
            for question in self.routes.questions[page]:
                fields.extend(_values(question, answer[question.index]))
        return pages, urlencode(fields).encode("utf-8")

    def _observe(self, observe, pages):
        if observe is None:
            return
        config = self.config
        if config.form_description:
            observe(0, "intro", [{"text": config.form_description}])
        for page in pages:
            blocks = [{"text": config.sections[page - 1]}] if config.sections else []
            blocks.extend(
                {"text": "\n".join(filter(None, (item.get("title"), item.get("description"))))}
                for item in config.instructions
                if item.get("page") == page
            )
            blocks.extend({"text": q.description} for q in self.routes.questions[page] if q.description)
            observe(page, "section", blocks)

    def validate(self, answer, *, observe=None):
        pages, _ = self._prepare(answer)
        self._observe(observe, pages)
        return True, "HTTP request validated; nothing sent"

    def submit(self, answer, *, before_submit=None, observe=None):
        pages, body = self._prepare(answer)
        self._observe(observe, pages)
        if before_submit is not None:
            before_submit()
        try:
            status, headers, document = self._request("POST", self.endpoint, body)
            # A redirect after POST is never followed by another POST. A single
            # same-form GET may retrieve confirmation without repeating intent.
            if status in {301, 302, 303} and headers.get("Location"):
                destination = urljoin(self.endpoint, headers["Location"])
                parsed = urlsplit(destination)
                expected = urlsplit(self.endpoint)
                if (parsed.scheme, parsed.netloc, parsed.path) != (
                    expected.scheme,
                    expected.netloc,
                    expected.path,
                ):
                    raise SubmissionUncertainError(
                        "Google redirected the submission outside its expected confirmation page."
                    )
                status, headers, document = self._request("GET", destination)
            page = _Page(document)
            if page.errors or "pageHistory" in page.hidden and status in {200, 400}:
                raise SubmissionRejectedError(
                    "Google redisplayed the form instead of accepting it. Check required answers and validation."
                )
            if status == 400:
                raise SubmissionRejectedError(
                    "Google rejected the response (HTTP 400). Re-inspect the form before retrying."
                )
            raise_for_access(document, self.endpoint, status)
            expected = " ".join(self.config.confirmation_text.split())
            confirmed = (
                status == 200
                and not page.challenge
                and not page.controls
                and any(
                    " ".join(block["text"].split()) == expected if expected else bool(block["text"].strip())
                    for block in page.confirmations
                )
            )
            if not confirmed:
                raise SubmissionUncertainError(
                    "Google did not provide a recognizable acceptance confirmation. Check responses before retrying."
                )
        except SubmissionRejectedError:
            raise
        except FormAccessError as exc:
            raise SubmissionUncertainError(
                f"{exc} The attempted response is uncertain; check responses before retrying."
            ) from exc
        except BaseException as exc:
            if isinstance(exc, SubmissionUncertainError):
                raise
            raise SubmissionUncertainError(
                "Submission may have reached Google, but acceptance could not be confirmed. Check responses before retrying."
            ) from exc
        if observe is not None:
            # Observation failures cannot turn an accepted response into a retry.
            try:
                observe(0, "confirmation", page.confirmations)
            except Exception:
                pass
        return True, "submitted; Google confirmation received"

    def close(self):
        self.pool.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
