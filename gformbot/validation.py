"""Supported email and calendar constraints shared by preparation and execution."""

import re
from datetime import datetime

# HTML email input syntax; unlike a delivery check, this needs no network.
EMAIL_PATTERN = r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*"

CHECKBOX_RULES = {200: "minimum", 201: "maximum", 204: "exact"}


def is_checkbox_rule(rule):
    return (
        isinstance(rule, list)
        and len(rule) >= 3
        and type(rule[0]) is int
        and rule[0] == 7
        and type(rule[1]) is int
        and rule[1] in CHECKBOX_RULES
    )


def checkbox_bounds(question):
    """Intersect verified Google rules with existing explicit selection limits."""
    minimum, maximum = question.min_select, question.max_select
    for rule in question.validation:
        if not is_checkbox_rule(rule):
            continue
        args = rule[2]
        if (
            not isinstance(args, list)
            or len(args) != 1
            or type(args[0]) not in (str, int)
            or not re.fullmatch(r"\d+", str(args[0]))
        ):
            raise ValueError(
                f"Invalid checkbox selection constraint for {question.text!r}; inspection stopped."
            )
        n = int(args[0])
        kind = CHECKBOX_RULES[rule[1]]
        if kind in ("minimum", "exact"):
            minimum = max(minimum, n)
        if kind in ("maximum", "exact"):
            maximum = n if maximum is None else min(maximum, n)
    if minimum > len(question.options) or maximum is not None and maximum < minimum:
        raise ValueError(f"Impossible checkbox selection constraints for {question.text!r}.")
    return minimum, maximum


def date_settings(flags):
    """DateOptions omits defaults: includes_time=false, includes_year=true."""
    if (
        not isinstance(flags, list)
        or len(flags) > 2
        or any(
            value is not None and (type(value) not in (int, bool) or value not in (0, 1)) for value in flags
        )
    ):
        raise ValueError("Unrecognized date settings.")
    return tuple(
        bool(flags[i]) if i < len(flags) and flags[i] is not None else default
        for i, default in enumerate((False, True))
    )


def is_email_rule(rule):
    return (
        isinstance(rule, list)
        and len(rule) >= 2
        and rule[:2] == [2, 102]
        and (len(rule) < 3 or rule[2] in (None, []))
    )


def needs_email(question):
    return any(is_email_rule(rule) for rule in question.validation)


def date_format(question):
    return ("YYYY-MM-DD" if question.date_include_year else "MM-DD") + (
        " HH:MM" if question.date_include_time else ""
    )


def date_pattern(question):
    return (r"\d{4}-\d{2}-\d{2}" if question.date_include_year else r"\d{2}-\d{2}") + (
        r" \d{2}:\d{2}" if question.date_include_time else ""
    )


def parse_date(question, value):
    value = value.replace("T", " ")
    if not re.fullmatch(date_pattern(question), value):
        raise ValueError(f"Use {date_format(question)} for {question.text!r}.")
    calendar = value if question.date_include_year else "2000-" + value
    fmt = "%Y-%m-%d" + (" %H:%M" if question.date_include_time else "")
    try:
        parsed = datetime.strptime(calendar, fmt)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date or time for {question.text!r}: {value!r}.") from exc
    return value, parsed


class AmbiguousDateError(ValueError):
    """Numeric CSV dates need the user to identify the month/day order."""


def _csv_date(question, value, order):
    value = value.strip().replace("\u00a0", " ").replace("T", " ")
    if not value:
        return ""
    if re.fullmatch(date_pattern(question), value):
        return parse_date(question, value)[0]
    match = re.fullmatch(
        r"(\d{1,4})[/-](\d{1,2})(?:[/-](\d{1,4}))?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(AM|PM))?)?",
        value,
        re.I,
    )
    if not match:
        raise ValueError(
            f"Use {date_format(question)} or a Google Forms export date for {question.text!r}: {value!r}."
        )
    first, second, third, hour, minute, seconds, period = match.groups()
    if len(first) == 4:
        year, month, day = int(first), int(second), int(third or 0)
    else:
        month, day = (int(first), int(second)) if order == "mdy" else (int(second), int(first))
        if third and len(third) != 4:
            raise ValueError(f"CSV dates must use a four-digit year for {question.text!r}.")
        year = int(third) if third else 2000
        if question.date_include_year and third is None:
            raise ValueError(f"A year is required for {question.text!r}.")
    if question.date_include_time and hour is None:
        raise ValueError(f"A time is required for {question.text!r}; use {date_format(question)}.")
    h, m, s = int(hour or 0), int(minute or 0), int(seconds or 0)
    if period:
        if not 1 <= h <= 12:
            raise ValueError(f"Invalid 12-hour clock for {question.text!r}.")
        h = h % 12 + (12 if period.upper() == "PM" else 0)
    if s:
        raise ValueError(
            f"Nonzero seconds cannot be represented by {question.text!r}; the form accepts minutes only."
        )
    if not question.date_include_time and (h or m):
        raise ValueError(f"{question.text!r} accepts a date without a time.")
    try:
        parsed = datetime(year, month, day, h, m)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date or time for {question.text!r}: {value!r}.") from exc
    canonical = parsed.date().isoformat() if question.date_include_year else f"{month:02d}-{day:02d}"
    if question.date_include_time:
        canonical += f" {h:02d}:{m:02d}"
    return canonical


def normalize_csv_dates(question, values, order=None, *, row_numbers=None):
    """Infer one consistent date order for an entire column, never per cell."""
    possibilities, errors = {}, []
    numbers = row_numbers if row_numbers is not None else range(2, len(values) + 2)
    for candidate in [order] if order else ["mdy", "dmy"]:
        normalized = []
        for row, value in zip(numbers, values, strict=True):
            try:
                normalized.append(_csv_date(question, value, candidate))
            except ValueError as exc:
                errors.append(f"CSV row {row}: {exc}")
                break
        else:
            possibilities[candidate] = normalized
    if not possibilities:
        raise ValueError(" ".join(dict.fromkeys(errors)))
    if len(possibilities) == 2 and possibilities["mdy"] != possibilities["dmy"]:
        raise AmbiguousDateError(
            f"The CSV date order for {question.text!r} is ambiguous. Choose month/day/year or day/month/year; scripted commands accept --date-order mdy or dmy."
        )
    return next(iter(possibilities.values()))
