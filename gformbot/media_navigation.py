"""Discover public media through real Next navigation, without submitting a form."""

import re
from collections import deque
from datetime import date
from decimal import Decimal
from time import monotonic
from urllib.parse import urlencode, urljoin, urlsplit

import certifi
import urllib3

from .access import raise_for_access, require_public_form
from .generator import CHOICE_TYPES, validate_answer
from .grids import grid_groups, random_grid
from .inspector import parse_form_html, schema_fingerprint
from .media import media_links
from .routing import Routing
from .transport import _Page, _values
from .validation import text_rules, validate_text

_LIMIT = 10_000_000
_USER_AGENT = "Mozilla/5.0 (compatible; GFormBot media preview)"


class _NavigationBlocked(ValueError):
    pass


class _NavigationPage(_Page):
    def __init__(self, document):
        self.next_control = False
        self.next_depth = None
        self.next_text = []
        self.action = ""
        super().__init__(document)
        self.next_control = self.next_control or " ".join("".join(self.next_text).split()) == "Next"

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.action = values.get("action", "")
        parent_ignored = self.stack[-1][1] if self.stack else False
        visible = (
            not parent_ignored
            and "hidden" not in values
            and values.get("aria-hidden") != "true"
            and not re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", values.get("style", ""), re.I)
        )
        if visible and values.get("name") == "continue" and tag in {"button", "input"}:
            self.next_control = values.get("value") == "1"
        if visible and values.get("role") == "button" and values.get("jsname") == "OCpkoe":
            self.next_depth = len(self.stack)
        super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.next_depth is not None and self.stack and not self.stack[-1][1]:
            self.next_text.append(data)
        super().handle_data(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if self.next_depth is not None and len(self.stack) <= self.next_depth:
            self.next_depth = None


def _text_value(question, stop_event=None):
    """Try bounded, cheap candidates; every candidate passes the shared validator."""
    rules = text_rules(question)
    candidates = ["Preview", "0", "1", "10", "Preview123!", "preview@example.test", "https://example.test"]
    contains = [r.args[0] for r in rules if r.family == 2 and r.operation == 100]
    if contains:
        candidates.insert(0, " ".join(contains))
    for rule in rules:
        if rule.family == 4 and rule.operation in (299, 301):
            # A cheap seed for common literal/code patterns, never a regex solver.
            seed = re.sub(r"\[([A-Za-z0-9])(?:-[A-Za-z0-9])?\]", r"\1", rule.args[0])
            seed = seed.replace(r"\d", "0").replace(r"\w", "A").replace(r"\s", " ")
            seed = re.sub(r"([^\\])\{([0-9]+)(?:,[0-9]*)?\}", lambda m: m[1] * min(int(m[2]), 4096), seed)
            candidates.insert(0, seed.removeprefix("^").removesuffix("$"))
    bounds = [n for rule in rules if rule.family == 1 for n in rule.args]
    for number in bounds:
        candidates.extend(str(v) for v in (number, number + 1, number - 1))
    if bounds:
        candidates.append(str((min(bounds) + max(bounds)) / Decimal(2)))
    minimum = max((r.args[0] for r in rules if r.family == 6 and r.operation == 203), default=1)
    maximum = min((r.args[0] for r in rules if r.family == 6 and r.operation == 202), default=4096)
    if minimum > min(maximum, 4096):
        raise ValueError(f"Q{question.index}: the preview cannot create a short valid temporary answer.")
    for original in candidates:
        if stop_event is not None and stop_event.is_set():
            raise ValueError("Media loading stopped.")
        value = original[:maximum]
        value += "x" * max(0, minimum - len(value))
        try:
            validate_text(question, value)
            return value
        except ValueError as exc:
            if "timed out" in str(exc):
                raise
            continue
    raise ValueError(f"Q{question.index}: preview navigation needs an answer satisfying its validation.")


def _page_answers(config, routes, page, preset, stop_event=None):
    values = dict(preset)
    groups = grid_groups(config)
    for question in routes.questions[page]:
        if stop_event is not None and stop_event.is_set():
            raise ValueError("Media loading stopped.")
        if question.index in values:
            continue
        if question.grid_unique:
            if not all(row.index in values for row in groups[question.item_id]):
                values.update(random_grid(groups[question.item_id], values))
            continue
        if not question.required:
            value = [] if question.qtype == "checkbox" else ""
        elif question.qtype == "checkbox":
            minimum = max(1, question.min_select)
            options = [o for o in question.options if minimum == 1 or o not in question.exclusive_options]
            value = options[:minimum]
        elif question.qtype in CHOICE_TYPES:
            value = next(iter(question.options), "")
        elif question.qtype in {"text", "textarea"}:
            value = _text_value(question, stop_event)
        elif question.qtype == "date":
            value = date.today().isoformat()
            if not question.date_include_year:
                value = value[5:]
            if question.date_include_time:
                value += " 12:00"
        elif question.qtype in {"time", "duration"}:
            value = "12:00" if question.qtype == "time" else "00:00:01"
        else:
            raise ValueError(f"Q{question.index}: preview navigation cannot answer this control.")
        values[question.index] = value
    routes.require_supported(values)
    return validate_answer(config, values, partial=True, routing=routes) if values else values


def _edges(routes, page):
    if page == len(routes.questions):
        return {}
    outcomes = {routes.destinations[page - 1]: {}}
    for question in routes.questions[page]:
        if not question.branches and question.other_branch is None:
            continue
        following = {}
        options = [
            (value, question.branches.get(value))
            for value in ([""] if not question.required else []) + question.options
        ]
        if question.allow_other and question.other_branch is not None:
            options.append(({"choices": [], "other": "Preview"}, question.other_branch))
        for value, destination in options:
            for target, choices in outcomes.items():
                following.setdefault(
                    target if destination is None else destination, {**choices, question.index: value}
                )
        outcomes = following
    return {target: values for target, values in outcomes.items() if target}


def _path(config, routes, start, targets, visited, blocked=(), stop_event=None):
    """Breadth-first search minimizes actual Next requests, respecting valid answers."""
    queue = deque([(start, [])])
    seen = set(visited) - {start}
    failures = []
    while queue:
        if stop_event is not None and stop_event.is_set():
            return None, failures
        page, path = queue.popleft()
        if page in seen or page in blocked:
            continue
        seen.add(page)
        if page in targets:
            return path, failures
        for target, choices in _edges(routes, page).items():
            if target in seen:
                continue
            try:
                values = _page_answers(config, routes, page, choices, stop_event)
            except ValueError as exc:
                failures.append(str(exc))
                continue
            queue.append((target, [*path, (page, target, values)]))
    return None, failures


def discover(config, initial_document, stop_event, publish):
    """Publish newly resolved links from a temporary, cancellable public-form session."""
    require_public_form(config)
    routes = Routing(config)
    routes.require_supported(())
    links = {}

    def observe(document):
        if not stop_event.is_set():
            found = media_links(config.media, document, page=None)
            if found and not stop_event.is_set():
                links.update(found)
                publish(found)

    observe(initial_document)
    if len(links) == len(config.media) or stop_event.is_set():
        return
    pool = urllib3.PoolManager(
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        timeout=urllib3.Timeout(connect=3, read=5),
        retries=False,
        headers={"User-Agent": _USER_AGENT},
    )
    endpoint = config.url.rsplit("/", 1)[0] + "/formResponse"
    deadline = monotonic() + 60
    fingerprint = schema_fingerprint(config)

    def fetch(method, url, body=None):
        if stop_event.is_set():
            return None
        if monotonic() >= deadline:
            raise ValueError("Media preview reached its time limit. Available items remain viewable.")
        response = pool.request(
            method,
            url,
            body=body,
            headers={"User-Agent": _USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
            if body is not None
            else None,
            redirect=False,
            retries=False,
            preload_content=False,
        )
        try:
            data = response.read(_LIMIT + 1)
            if stop_event.is_set():
                return None
            if len(data) > _LIMIT:
                raise ValueError("The media responder page is too large.")
            document = data.decode("utf-8-sig")
            raise_for_access(document, url, response.status)
            if response.status != 200:
                raise ValueError(f"Media preview could not navigate this form (HTTP {response.status}).")
            current = parse_form_html(document, config.url)
            require_public_form(current)
            if schema_fingerprint(current) != fingerprint:
                raise ValueError("The form changed while its media was loading.")
            return document
        finally:
            response.close()
            response.release_conn()

    def advance(document, previous, target, values):
        page = _NavigationPage(document)
        actual = page.hidden.get("pageHistory", "").split(",")
        if (
            previous == len(routes.questions)
            or not page.next_control
            or page.challenge
            or any(error.strip() for error in page.errors)
            or page.confirmations
            or not page.hidden.get("fbzx")
            or not actual
            or any(not part.isdecimal() for part in actual)
            or int(actual[-1]) != previous - 1
            or urlsplit(urljoin(config.url, page.action)) != urlsplit(endpoint)
        ):
            raise _NavigationBlocked("A verified Next control is unavailable; this preview route stopped.")
        fields = [(key, value) for key, value in page.hidden.items() if key not in {"continue", "back"}]
        fields.append(("continue", "1"))
        for question in routes.questions[previous]:
            fields.extend(_values(question, values[question.index]))
        document = fetch("POST", endpoint, urlencode(fields).encode("utf-8"))
        if document is None:
            return None
        returned = _NavigationPage(document)
        if returned.challenge or returned.confirmations:
            raise ValueError("Google could not verify a media navigation step.")
        expected = [*actual, str(target - 1)]
        if (
            any(error.strip() for error in returned.errors)
            or returned.hidden.get("pageHistory", "").split(",") != expected
        ):
            raise _NavigationBlocked(
                "Google did not accept the expected Next step; this preview route stopped."
            )
        return document

    document, current, visited = initial_document, 1, set()
    examined, blocked, limitations = set(), set(), []
    try:
        while not stop_event.is_set():
            if document is None:
                document = fetch("GET", config.url)
                if document is None:
                    return
                observe(document)
            unresolved = [asset for asset in config.media if asset["key"] not in links]
            if not unresolved:
                return
            examined.add(current)
            targets = {asset["page"] for asset in unresolved} - examined
            visited.add(current)
            path, failures = _path(config, routes, current, targets, visited, blocked, stop_event)
            if path is None and current != 1:
                path, more = _path(config, routes, 1, targets, set(), blocked, stop_event)
                failures.extend(more)
                if path is not None:
                    document = fetch("GET", config.url)
                    if document is None:
                        return
                    current, visited = 1, set()
                    observe(document)
            if stop_event.is_set():
                return
            if not path:
                detail = next(
                    iter([*limitations, *failures]), "Some media has no reachable public preview link."
                )
                raise ValueError(detail)
            for previous, target, values in path:
                if stop_event.is_set():
                    return
                try:
                    document = advance(document, previous, target, values)
                except _NavigationBlocked as exc:
                    if previous == 1:
                        raise ValueError(str(exc)) from exc
                    limitations.append(f"Section {previous}: {exc}")
                    blocked.add(previous)
                    document, current, visited = None, 1, set()
                    break
                if document is None:
                    return
                current = target
                visited.add(previous)
                examined.add(target)
                observe(document)
    except (urllib3.exceptions.HTTPError, OSError, UnicodeError) as exc:
        if not stop_event.is_set():
            raise ValueError("Media preview lost its connection. Available items remain viewable.") from exc
    finally:
        pool.clear()
