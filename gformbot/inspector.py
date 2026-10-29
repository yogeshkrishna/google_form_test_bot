"""Read a public responder page's complete schema without advancing or submitting.

Google embeds all sections in FB_PUBLIC_LOAD_DATA_. This is an undocumented
format, so shape changes and unsupported controls are reported explicitly.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .access import FormAccessError, check_browser_access, raise_for_access
from .media import inspect_media
from .validation import checkbox_bounds, date_settings, is_checkbox_rule, text_rules, time_settings


@dataclass
class QuestionSchema:
    index: int
    text: str
    qtype: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    page: int = 1
    entry_id: str = ""
    item_id: str = ""
    description: str = ""
    min_select: int = 0
    max_select: int | None = None
    exclusive_options: list[str] = field(default_factory=list)
    validation: list = field(default_factory=list)
    date_include_year: bool = True
    date_include_time: bool = False
    branches: dict[str, int] = field(default_factory=dict)
    grid_title: str = ""
    grid_row: str = ""
    grid_unique: bool = False
    unsupported: list[str] = field(default_factory=list)
    rating_icon: str = ""
    allow_other: bool = False
    other_branch: int | None = None


@dataclass
class FormSchema:
    url: str
    title: str
    questions: list[QuestionSchema] = field(default_factory=list)
    pages: int = 1
    description: str = ""
    sections: list[str] = field(default_factory=lambda: ["Section 1"])
    unsupported: list[str] = field(default_factory=list)
    confirmation_text: str = ""
    instructions: list[dict] = field(default_factory=list)
    requires_signin: bool = False
    one_response: bool | None = None
    email_collection: str = "unknown"
    section_ids: list[str] = field(default_factory=list)
    section_next: list[int] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)

    def get(self, index: int) -> QuestionSchema | None:
        return next((q for q in self.questions if q.index == index), None)

    def require_supported(self) -> None:
        from .routing import Routing

        Routing(self).require_supported()
        if not self.questions:
            raise ValueError("No answerable questions were found in this form.")


KINDS = {
    0: "text",
    1: "textarea",
    2: "radio",
    3: "dropdown",
    4: "checkbox",
    5: "scale",
    9: "date",
    10: "time",
    18: "rating",
}
UNSUPPORTED = {13: "file upload"}


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")


def plain_text(value) -> str:
    value = str(value or "")
    if re.search(r"</?[a-zA-Z][^>]*>", value):
        parser = _PlainText()
        parser.feed(value)
        return html.unescape("".join(parser.parts)).strip()
    return html.unescape(value).strip()


def clean_url(value: str, allow_short=True) -> str:
    value = value.strip().strip("\"'").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Paste an HTTPS Google Forms responder URL (ending in /viewform).")
    if allow_short and parsed.hostname == "forms.gle":
        return value
    if parsed.hostname != "docs.google.com":
        raise ValueError("Use a docs.google.com/forms/.../viewform URL or a forms.gle short link.")
    match = re.fullmatch(r"(/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]+)/viewform/?", parsed.path)
    if not match:
        raise ValueError("Use the responder link ending in /viewform, rather than the editor link.")
    # Tracking and pre-filled query parameters must not affect a schema or run.
    return "https://docs.google.com" + match.group(1) + "/viewform"


def _get(seq, index, default=None):
    return seq[index] if isinstance(seq, list) and len(seq) > index else default


def _checkbox_rules(question: QuestionSchema) -> None:
    text = f"{question.text} {question.description}"
    # Infer only explicit natural-language instructions; never assume the last
    # option is exclusive just because of its position.
    if re.search(
        r"(?:either of |one of )?(?:the )?last two options?.{0,55}choose (?:it |them )?alone", text, re.I
    ):
        question.exclusive_options = question.options[-2:]
    elif re.search(r"(?:the )?last option.{0,55}choose (?:it )?alone", text, re.I):
        question.exclusive_options = question.options[-1:]
    match = re.search(r"(?:choose|select|pick)\s+(?:up to|at most|no more than)\s+(\d+)", text, re.I)
    if match:
        question.max_select = int(match.group(1))
    question.min_select, question.max_select = checkbox_bounds(question)


def parse_form_html(document: str, url: str) -> FormSchema:
    raise_for_access(document, url)
    marker = re.search(r"\bFB_PUBLIC_LOAD_DATA_\s*=\s*", document)
    if not marker:
        raise FormAccessError("unknown")
    try:
        data, _ = json.JSONDecoder().raw_decode(document[marker.end() :])
        meta = data[1]
        items = meta[1]
        if not isinstance(items, list):
            raise ValueError("missing question list")
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Google's form metadata has an unrecognized structure; inspection stopped.") from exc
    settings = _get(meta, 10)
    once = _get(settings, 1)
    one_response = (
        bool(once)
        if isinstance(settings, list)
        and len(settings) > 1
        and (once is None or type(once) in (bool, int) and once in (0, 1))
        else None
    )
    email = _get(settings, 6)
    email_collection = (
        {1: "none", 2: "verified", 3: "input"}.get(email, "unknown") if type(email) is int else "unknown"
    )
    form = FormSchema(
        url=url,
        title=plain_text(_get(meta, 8) or _get(data, 3) or "Google Form"),
        description=plain_text(_get(meta, 0)),
        questions=[],
        sections=["Section 1"],
        confirmation_text=plain_text(_get(_get(meta, 2), 0)),
        requires_signin=_get(data, 18) in (True, 1) or one_response is True or email_collection == "verified",
        one_response=one_response,
        email_collection=email_collection,
    )
    # Google renders responder-input email outside its embedded item list.
    # Account-verified collection remains restriction metadata, not an answer.
    if email_collection == "input":
        form.questions.append(
            QuestionSchema(
                1,
                "Email",
                "text",
                required=True,
                entry_id="emailAddress",
                item_id="emailAddress",
                validation=[[2, 102]],
            )
        )
    seen_ids = set()
    section_ids, endings = [-1], []
    for item in items:
        if not isinstance(item, list) or len(item) < 4:
            raise ValueError("Unrecognized question metadata; inspection stopped instead of guessing.")
        item_id, title, description, type_code = item[:4]
        # These fields are already plain text in Google's JSON. Treating
        # literal code/entity examples as HTML would change CSV headers.
        title, description = str(title or "").strip(), str(description or "").strip()
        form.media.extend(inspect_media(item, len(form.questions) + 1, len(form.sections)))
        if type_code == 8:
            if type(item_id) is not int or item_id < 0 or item_id in section_ids:
                raise ValueError("Unrecognized or duplicate section identity; inspection stopped.")
            section_ids.append(item_id)
            endings.append(_get(item, 5))
            form.sections.append(title or f"Section {len(form.sections) + 1}")
            if description:
                form.instructions.append(
                    {
                        "page": len(form.sections),
                        "before_question": len(form.questions) + 1,
                        "title": title,
                        "description": description,
                    }
                )
            continue
        if type_code == 6:
            form.instructions.append(
                {
                    "page": len(form.sections),
                    "before_question": len(form.questions) + 1,
                    "title": title,
                    "description": description,
                }
            )
            continue
        if type_code in (11, 12):  # media have no answer field
            continue
        entries = _get(item, 4)
        if type_code == 7:
            from .grids import parse_grid

            rows = parse_grid(item, len(form.questions), len(form.sections))
            for row in rows:
                if row.entry_id in seen_ids:
                    raise ValueError(f"Duplicate answer field ID {row.entry_id}; inspection stopped.")
                seen_ids.add(row.entry_id)
            form.questions.extend(rows)
            continue
        if type_code not in KINDS or not isinstance(entries, list) or len(entries) != 1:
            name = UNSUPPORTED.get(type_code, f"question type {type_code}")
            form.unsupported.append(f"{name}: {title}")
            form.questions.append(
                QuestionSchema(
                    len(form.questions) + 1,
                    title,
                    name,
                    page=len(form.sections),
                    item_id=str(item_id),
                    description=description,
                    unsupported=[form.unsupported[-1]] if type_code in (13, 18) else [],
                )
            )
            continue
        entry = entries[0]
        if not isinstance(entry, list) or len(entry) < 3:
            raise ValueError(f"Unrecognized answer field for {title!r}.")
        entry_id = str(entry[0])
        if entry_id in seen_ids:
            raise ValueError(f"Duplicate answer field ID {entry_id}; inspection stopped.")
        seen_ids.add(entry_id)
        raw_options = _get(entry, 1)
        option_data = [] if raw_options is None else raw_options
        if not isinstance(option_data, list) or any(
            not isinstance(opt, list) or not opt for opt in option_data
        ):
            raise ValueError(f"Unrecognized choices or routing metadata for {title!r}.")
        other_options = [opt for opt in option_data if _get(opt, 4) == 1]
        if len(other_options) > 1 or other_options and type_code not in (2, 4):
            raise ValueError(f"Unrecognized Other option metadata for {title!r}.")
        options = [str(opt[0]) for opt in option_data if _get(opt, 4) != 1]
        issue_start = len(form.unsupported)
        if any(not option.strip() for option in options):
            if any(_get(opt, 2) is not None for opt in option_data):
                raise ValueError(f"Ambiguous routing choices for {title!r}; inspection stopped.")
            form.unsupported.append(f"empty option labels: {title}")
        if len(options) != len(set(options)):
            if any(_get(opt, 2) is not None for opt in option_data):
                raise ValueError(f"Ambiguous routing choices for {title!r}; inspection stopped.")
            form.unsupported.append(f"duplicate option labels: {title}")
        raw_validation = _get(entry, 4)
        validation = [] if raw_validation is None else raw_validation
        if not isinstance(validation, list):
            raise ValueError(f"Unrecognized validation metadata for {title!r}.")
        q = QuestionSchema(
            len(form.questions) + 1,
            title,
            KINDS[type_code],
            bool(entry[2]),
            options=options,
            page=len(form.sections),
            entry_id=entry_id,
            item_id=str(item_id),
            description=description,
            min_select=1 if entry[2] and type_code == 4 else 0,
            validation=validation,
            branches={
                str(opt[0]): opt[2] for opt in option_data if _get(opt, 2) is not None and _get(opt, 4) != 1
            },
            allow_other=bool(other_options),
            other_branch=_get(other_options[0], 2) if other_options else None,
        )
        if q.qtype == "date":
            try:
                q.date_include_time, q.date_include_year = date_settings(_get(entry, 7))
            except ValueError:
                form.unsupported.append(f"unrecognized date settings: {title}")
        if q.qtype == "time":
            try:
                if time_settings(_get(entry, 6)):
                    q.qtype = "duration"
            except ValueError:
                form.unsupported.append(f"unrecognized time settings: {title}")
        if q.qtype == "rating":
            flags = _get(entry, 14)
            if (
                not isinstance(flags, list)
                or len(flags) > 1
                or flags
                and flags[0] is not None
                and (type(flags[0]) is not int or flags[0] not in (1, 2, 3))
                or not 3 <= len(options) <= 10
                or options != [str(i) for i in range(1, len(options) + 1)]
            ):
                form.unsupported.append(f"unrecognized rating settings: {title}")
            else:
                q.rating_icon = {1: "star", 2: "heart", 3: "thumb_up"}[
                    flags[0] if flags and flags[0] is not None else 1
                ]
        if q.qtype == "checkbox":
            _checkbox_rules(q)
            for rule in validation:
                if not is_checkbox_rule(rule):
                    form.unsupported.append(f"unrecognized checkbox validation: {title}")
        elif validation:
            try:
                if q.qtype not in ("text", "textarea"):
                    raise ValueError("This control does not support text validation.")
                text_rules(q)
            except ValueError as exc:
                form.unsupported.append(f"custom validation on {title!r}: {exc}")
        if q.qtype in ("radio", "checkbox", "dropdown", "scale") and not q.options and not q.allow_other:
            form.unsupported.append(f"missing option labels: {title}")
        q.unsupported = form.unsupported[issue_start:]
        form.questions.append(q)
    form.pages = len(form.sections)
    form.section_ids = [str(value) for value in section_ids]
    destinations = {value: page for page, value in enumerate(section_ids, 1)}

    def destination(value, page):
        if value is None or type(value) is int and value == -2:
            return page + 1 if page < form.pages else 0
        if type(value) is int and value == -3:
            return 0
        if type(value) is int and value in destinations:
            return destinations[value]
        raise ValueError(f"Unrecognized section destination {value!r}; inspect the form again.")

    next_pages = [destination(value, page) for page, value in enumerate(endings, 1)] + [0]
    if next_pages != list(range(2, form.pages + 1)) + [0]:
        form.section_next = next_pages
    for question in form.questions:
        question.branches = {
            option: destination(value, question.page) for option, value in question.branches.items()
        }
        if question.other_branch is not None:
            question.other_branch = destination(question.other_branch, question.page)
    from .routing import Routing

    Routing(form)
    return form


def inspect_form(
    url: str, browser: str = "auto", headless: bool = True, status_cb=None, timeout: int = 30, *, driver=None
) -> FormSchema:
    requested = clean_url(url)
    if status_cb:
        status_cb("Reading all form questions and constraints…")
    try:
        if driver is not None:
            driver.get(requested)
            check_browser_access(driver)
            final_url = clean_url(driver.current_url, allow_short=False)
            document = driver.page_source
            if len(document) > 10_000_000:
                raise ValueError("The responder page is too large to inspect safely.")
        else:
            request = Request(requested, headers={"User-Agent": "gformbot (form inspection)"})
            with urlopen(request, timeout=timeout) as response:
                raise_for_access(url=response.url, status=getattr(response, "status", None))
                raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise ValueError("The responder page is too large to inspect safely.")
                encoding = response.headers.get_content_charset() or "utf-8"
                document = raw.decode(encoding)
                final_url = clean_url(response.url, allow_short=False)
        schema = parse_form_html(document, final_url)
        if status_cb:
            status_cb(f"Read {len(schema.questions)} questions across {schema.pages} section(s).")
        return schema
    except HTTPError as exc:
        try:
            raise_for_access(url=exc.url, status=exc.code)
            raise FormAccessError("unknown") from exc
        finally:
            exc.close()
    except ValueError:
        raise
    except (URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise FormAccessError("temporary_failure") from exc


def schema_fingerprint(schema_or_config) -> str:
    """Compare form contents, ignoring response-generation preferences."""
    source = schema_or_config
    fields = (
        "index",
        "text",
        "qtype",
        "required",
        "options",
        "page",
        "entry_id",
        "item_id",
        "description",
        "min_select",
        "max_select",
        "exclusive_options",
        "validation",
    )
    payload = {
        "url": source.url,
        "title": getattr(source, "title", getattr(source, "form_title", "Google Form")),
        "description": getattr(source, "description", getattr(source, "form_description", "")),
        "sections": source.sections,
        "unsupported": source.unsupported,
        "confirmation_text": source.confirmation_text,
        "questions": [
            {
                **{key: getattr(q, key) for key in fields},
                **(
                    {"date_include_year": q.date_include_year, "date_include_time": q.date_include_time}
                    if q.qtype == "date"
                    else {}
                ),
                **(
                    {"grid_title": q.grid_title, "grid_row": q.grid_row, "grid_unique": q.grid_unique}
                    if q.grid_title
                    else {}
                ),
                **({"unsupported": q.unsupported} if q.unsupported else {}),
                **({"rating_icon": q.rating_icon} if q.qtype == "rating" else {}),
                **({"allow_other": True, "other_branch": q.other_branch} if q.allow_other else {}),
            }
            for q in source.questions
        ],
    }
    from .routing import Routing

    if source.media:
        payload["media"] = source.media
    if Routing(source).enabled:
        payload["routing"] = {
            "section_ids": source.section_ids,
            "section_next": source.section_next,
            "choices": {str(q.index): q.branches for q in source.questions if q.branches},
        }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
