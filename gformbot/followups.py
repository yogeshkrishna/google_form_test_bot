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


def display(records, ui, *, full=False, warning="", log_path=None):
    """Group repeated directions and print literal text, safe on older Windows consoles."""
    try:
        groups = {}
        for record in records:
            block = record["content"]
            default = block["text"].strip().lower().rstrip(".! ") == "your response has been recorded"
            if not full and not block["links"] and (record["stage"] != "confirmation" or default):
                continue
            key = json.dumps(block, sort_keys=True, ensure_ascii=False)
            if key not in groups:
                groups[key] = (block, defaultdict(set))
            source = (record["batch"], record["title"], record["stage"], record["section"])
            groups[key][1][source].add(record["response"])
        lines = ["Next steps" if not full else "Saved form instructions"] if groups else []
        for block, sources in groups.values():
            for (batch, title, stage, section), ids in sources.items():
                numbers = ", ".join(map(str, sorted(ids)[:8])) + (
                    f" ({len(ids)} total)" if len(ids) > 8 else ""
                )
                location = (
                    "After submission"
                    if stage == "confirmation"
                    else "Introduction"
                    if stage == "intro"
                    else f"Section {section}"
                )
                lines.append(
                    f"{title} | Response {numbers} | {location}" + (f" | batch {batch[:8]}" if full else "")
                )
            if block["text"]:
                lines.append(block["text"])
            for link in block["links"]:
                if link not in block["text"]:
                    lines.append(link + (" [redacted; may not work]" if "REDACTED" in link else ""))
            if "REDACTED" in block["text"]:
                lines.append("Redacted links may not work.")
        if records and not full:
            command = "gformbot log --followups"
            if log_path and str(log_path) != "gformbot-runs.csv":
                command = f'gformbot log "{log_path}" --followups'
            lines.append("Form instructions: " + command)
        if full and not records:
            lines.append("No saved form instructions yet.")
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
