"""Observe form directions without changing submission or recovery decisions."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_SECRETS = {
    "token",
    "key",
    "sig",
    "password",
    "auth",
    "session",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authorization",
    "signature",
    "secret",
    "clientsecret",
}
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def safe_url(value: str, base: str, _depth: int = 0) -> str:
    """Keep public destinations; remove credentials and redact known secret parameters."""
    try:
        value = _CONTROL.sub("", value).strip()
        if value in {"", "#"}:
            return ""
        parsed = urlsplit(urljoin(base, value))
        if parsed.scheme not in {"http", "https", "mailto", "tel"}:
            return ""
        if parsed.username is not None or (parsed.scheme in {"http", "https"} and not parsed.hostname):
            return ""
        host = (parsed.hostname or "").lower()
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if host in {"google.com", "www.google.com"} and parsed.path == "/url":
            if _depth >= 3:
                return ""
            target = next((v for k, v in query if k in {"q", "url"}), "")
            return safe_url(target, base, _depth + 1) if target else ""
        if host == "accounts.google.com" or (
            host == "docs.google.com" and any(k.lower() == "edit2" for k, _ in query)
        ):
            return ""
        origin = urlsplit(base)
        if origin.path.rstrip("/").endswith("/formResponse") and (parsed.scheme, parsed.netloc) == (
            origin.scheme,
            origin.netloc,
        ):
            root = origin.path.rstrip("/").rsplit("/", 1)[0]
            if parsed.path.rstrip("/") in {root + "/viewform", root + "/formResponse"}:
                return ""

        def redact(raw):
            pairs = parse_qsl(raw, keep_blank_values=True)
            if not any(re.sub(r"[-_]", "", k).lower() in _SECRETS for k, _ in pairs):
                return raw
            return urlencode(
                [(k, "REDACTED" if re.sub(r"[-_]", "", k).lower() in _SECRETS else v) for k, v in pairs]
            )

        return urlunsplit(parsed._replace(query=redact(parsed.query), fragment=redact(parsed.fragment)))
    except (ValueError, TypeError):
        return ""


def redact_urls(text, base=""):
    """Apply the known URL-secret policy to text without changing other content."""

    def replace_url(match):
        raw = match[0].rstrip(".,;")
        return (safe_url(raw, base) or "[link omitted]") + match[0][len(raw) :]

    return _URL.sub(replace_url, text)


def _sanitize(block, base):
    text = _CONTROL.sub("", block.get("text", "")).strip()
    candidates = [*block.get("links", []), *(value.rstrip(".,;") for value in _URL.findall(text))]
    text = redact_urls(text, base)
    links = list(dict.fromkeys(url for raw in candidates if (url := safe_url(raw, base))))
    return {"text": text, "links": links}


def _instruction_lines(text, links):
    lines = [text] if text else []
    for link in links:
        if link not in text:
            lines.append(link + (" [redacted; may not work]" if "REDACTED" in link else ""))
    if "REDACTED" in text:
        lines.append("Redacted links may not work.")
    return lines


def _details(records):
    if not records:
        return ["No saved form instructions yet."]
    groups = defaultdict(lambda: defaultdict(set))
    for record in records:
        block = record["content"]
        source = (record["batch"], record["title"], record["stage"], record["section"])
        groups[block["text"], tuple(block["links"])][source].add(record["response"])
    lines = ["Saved form instructions"]
    for (text, links), sources in groups.items():
        for (batch, title, stage, section), ids in sources.items():
            numbers = ", ".join(map(str, sorted(ids)[:8])) + (f" ({len(ids)} total)" if len(ids) > 8 else "")
            location = (
                "After submission"
                if stage == "confirmation"
                else "Introduction"
                if stage == "intro"
                else f"Section {section}"
            )
            lines.append(f"{title} | Response {numbers} | {location} | batch {batch[:8]}")
        lines.extend(_instruction_lines(text, links))
    return lines


def _response_scope(ids):
    if len(ids) == 1:
        return f"Response {next(iter(ids))}"
    if len(ids) > 8:
        return f"{len(ids)} responses"
    return "Responses " + ", ".join(map(str, sorted(ids)))


def _summary(records, log_path):
    confirmations = defaultdict(set)
    actions = defaultdict(lambda: defaultdict(set))
    confirmed = set()
    observed = set()
    for record in records:
        stage, response = record["stage"], record["response"]
        observed.add(response)
        text, links = record["content"]["text"], record["content"]["links"]
        if stage == "confirmation":
            confirmed.add(response)
            if text and text.casefold().rstrip(".! ") != "your response has been recorded":
                confirmations[text].add(response)
            text = ""  # The confirmation text is already shown above its links.
        if links:
            section = record["section"] if stage == "section" else None
            actions[text, tuple(links)][section].add(response)
    lines = ["Confirmation message"] if confirmations else []
    for text, ids in confirmations.items():
        if ids != confirmed:
            lines.append(_response_scope(ids))
        lines.extend(_instruction_lines(text, ()))
    if actions:
        lines.append("Next steps")
        for (text, links), sources in actions.items():
            for section, ids in sources.items():
                labels = [f"Section {section}"] if section is not None else []
                if ids != observed:
                    labels.append(_response_scope(ids))
                if labels:
                    lines.append(" · ".join(labels))
            lines.extend(_instruction_lines(text, links))
        command = "gformbot log --followups"
        if log_path and str(log_path) != "gformbot-runs.csv":
            command = f'gformbot log "{log_path}" --followups'
        lines.append("Form instructions: " + command)
    return lines


def display(records, ui, *, full=False, warning="", log_path=None):
    """Group repeated directions and print literal text, safe on older Windows consoles."""
    try:
        lines = _details(records) if full else _summary(records, log_path)
        if warning:
            lines.append(warning)
        if lines:
            message = "\n".join(lines)
            console = getattr(ui, "console", None)
            encoding = getattr(getattr(console, "file", None), "encoding", None)
            if encoding:
                message = message.encode(encoding, errors="backslashreplace").decode(encoding)
            ui._info(message)
    except Exception:
        # A closed output stream cannot undo a confirmed submission.
        pass


class Recorder:
    def __init__(self, journal, log_path, title):
        self.journal, self.log_path = journal, log_path
        self.title = _sanitize({"text": title}, "")["text"]
        self.records = {}
        self.warning = ""

    def record(self, blocks, response, section, stage, base):
        """Store scoped HTTP instructions using the shared privacy policy."""
        try:
            by_stage = defaultdict(list)
            for raw in blocks:
                block = _sanitize(raw, base)
                if not block["text"] and not block["links"]:
                    continue
                observed_stage = raw.get("stage", stage)
                observed_section = 0 if observed_stage in {"intro", "confirmation"} else section
                key = (response, observed_section, observed_stage, json.dumps(block, sort_keys=True))
                self.records[key] = {
                    "batch": self.journal.identity,
                    "response": response,
                    "section": observed_section,
                    "stage": observed_stage,
                    "content": block,
                    "title": self.title,
                }
                by_stage[observed_section, observed_stage].append(block)
            for (observed_section, observed_stage), values in by_stage.items():
                self.journal.record_followups(
                    response, observed_section, observed_stage, values, self.log_path, self.title
                )
        except Exception:
            self.warning = (
                "Some form instructions could not be captured or saved. Submission status is unchanged."
            )

    def show(self, ui):
        display(list(self.records.values()), ui, warning=self.warning, log_path=self.log_path)
