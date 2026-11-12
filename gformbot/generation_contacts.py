"""Recognize and check generated contact details without contacting anyone."""

import re
import unicodedata

from .validation import EMAIL_PATTERN, needs_email

_QUESTION_PREFIX = re.compile(r"^(?:s\d+-q\d+|q\d+)\s*[\s:.—–-]+\s*", re.I)
_QUESTION_REQUEST = re.compile(r"^(?:(?:please )?(?:enter|provide|list) |what is )", re.I)
_CONTACT_PREFIX = (
    r"(?:(?:your|a|an|the|primary|alternate|alternative|secondary|personal|work|home|office|"
    r"business|emergency|parent(?:'s)?|guardian(?:'s)?|customer(?:'s)?|applicant(?:'s)?|"
    r"recipient(?:'s)?|contact|billing|shipping|owner(?:'s)?|respondent(?:'s)?|preferred|"
    r"daytime|evening)\s+)*"
)
_EMAIL_LABEL = re.compile(_CONTACT_PREFIX + r"e[- ]?mail(?: address)?", re.I)
_PHONE_LABEL = re.compile(
    _CONTACT_PREFIX + r"(?:(?:mobile|cell|home|work|office) )?"
    r"(?:phone|telephone|mobile|cell(?:phone)?|contact|whatsapp)(?: (?:number|no\.?|#))?",
    re.I,
)
_NON_CONTACT = re.compile(r"\b(?:probe|placeholder|dummy)\b", re.I)
_QUALIFIER = re.compile(r"(?:\s+\([^()]*\)|\s+\[[^\[\]]*\])+$")
_EXAMPLE_DOMAINS = {"example.com", "example.net", "example.org", "localhost"}
_RESERVED_TLDS = {"example", "invalid", "localhost", "test"}
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
_EXAMPLE_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:use|enter|provide)\b[^\n?!]{0,64}\b(?:example\.(?:com|net|org)|placeholder)\b",
    re.I | re.M,
)
_PHONE_VALUE = re.compile(r"\+?[\d(). \t\u00a0\u202f\-\u2011\u2013]+")
_PHONE_SEQUENCE = "01234567890123456789"


class ContactAnswerError(ValueError):
    def __init__(self, problems: dict[int, str]):
        self.keys = tuple(str(key) for key in problems)
        super().__init__(" ".join(f"Q{key}: {reason}" for key, reason in problems.items()))


def contact_kind(question) -> str | None:
    """Identify a single contact entry, excluding questions about contacts."""
    if question.qtype != "text":
        return None
    label = _QUESTION_PREFIX.sub("", question.text.strip())
    label = _QUESTION_REQUEST.sub("", label).rstrip(" :?*")
    label = _QUALIFIER.sub("", label)
    if _NON_CONTACT.search(label) or _EXAMPLE_REQUEST.search(question.description):
        return None
    if _EMAIL_LABEL.fullmatch(label) or needs_email(question):
        return "email"
    if _PHONE_LABEL.fullmatch(label) and label.casefold() != "contact":
        return "phone"
    return None


def _email_parts(value):
    if not isinstance(value, str) or not re.fullmatch(EMAIL_PATTERN, value.strip()):
        return None
    local, domain = value.strip().casefold().split("@", 1)
    if len(local) > 64 or len(domain) > 253 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return local, domain


def _phone_parts(value):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value).strip()
    if not _PHONE_VALUE.fullmatch(value):
        return None
    if value.count("(") + value.count(")") and (
        value.count("(") != 1 or value.count(")") != 1 or not re.search(r"\(\d{1,5}\)", value)
    ):
        return None
    digits = "".join(str(int(char)) for char in value if char.isdecimal())
    if value.startswith("+"):
        return digits, True
    if digits.startswith("00"):
        return digits[2:], True
    return digits, False


def contact_identity(kind, value) -> str | None:
    """Return a comparison key for one contact, ignoring common display aliases."""
    if kind == "email" and (parts := _email_parts(value)):
        local, domain = parts
        if domain in _GMAIL_DOMAINS:
            local = local.partition("+")[0].replace(".", "")
            domain = "gmail.com"
        return f"{local}@{domain}" if local else None
    if kind == "phone" and (parts := _phone_parts(value)):
        digits, international = parts
        if digits:
            return f"+{digits}" if international else digits
    return None


def _placeholder_phone(digits, international):
    # A calling code can occupy one to three digits. Check for dummy patterns
    # without guessing which national numbering plan the number belongs to.
    for offset in range(4 if international else 1):
        candidate = digits[offset:]
        if len(candidate) >= 4 and (
            candidate == (candidate[:2] * len(candidate))[: len(candidate)]
            or candidate in _PHONE_SEQUENCE
            or candidate in _PHONE_SEQUENCE[::-1]
        ):
            return True
    return False


def contact_problem(kind, value) -> str | None:
    """Check format and obvious stand-ins for an optional generated contact."""
    if value is None or value == "":
        return None
    if kind == "email":
        parts = _email_parts(value)
        if parts is None or "." not in parts[1] or contact_identity(kind, value) is None:
            return "Use a complete email address with a usable handle and domain."
        local, domain = parts
        if (
            domain in _EXAMPLE_DOMAINS
            or any(domain.endswith("." + example) for example in _EXAMPLE_DOMAINS)
            or domain.rsplit(".", 1)[-1] in _RESERVED_TLDS
            or local in {"dummy", "placeholder", "yourname", "test", "someone"}
        ):
            return "Use a natural address at an appropriate mail provider or organization."
        if domain in _GMAIL_DOMAINS:
            handle = local.partition("+")[0].replace(".", "")
            if not re.fullmatch(r"[a-z0-9]{6,30}", handle) or handle in {"postmaster", "abuse"}:
                return "Use a Gmail handle with 6 to 30 letters or digits and optional dots."
    if kind == "phone":
        parts = _phone_parts(value)
        if parts is None:
            return "Use a complete phone number formatted for the respondent's location."
        digits, international = parts
        if not 4 + int(international) <= len(digits) <= 15:
            return "Use a complete phone number formatted for the respondent's location."
        if international and digits.startswith("0"):
            return "Use a country calling code beginning with a nonzero digit."
        if _placeholder_phone(digits, international):
            return "Use a plausible phone number without a repeated or sequential placeholder pattern."
    return None
