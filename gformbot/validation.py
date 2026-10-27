"""Google Forms constraints shared by preparation and execution."""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache

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
    if (
        minimum > len(question.options) + int(question.allow_other)
        or maximum is not None
        and maximum < minimum
    ):
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


_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(_NUMBER, value.strip()):
        raise ValueError("Enter a finite number without grouping separators.")
    try:
        number = Decimal(value.strip())
        floating = float(number)
        if not number.is_finite() or not math.isfinite(floating) or number and not floating:
            raise ValueError("Number is outside the supported finite range.")
        return number
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Enter a finite number.") from exc


@dataclass(frozen=True)
class TextRule:
    family: int
    operation: int
    args: tuple
    description: str
    message: str = ""
    pattern: str = ""
    compiled: str = ""


def text_rules(question):
    if not question.validation:
        return ()
    try:
        return _text_rules(question.qtype, json.dumps(question.validation, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{question.text!r}: {exc}") from exc


@lru_cache(maxsize=256)
def _text_rules(kind, encoded):
    normalized = []
    for rule in json.loads(encoded):
        if not isinstance(rule, list) or not 2 <= len(rule) <= 4 or any(type(v) is not int for v in rule[:2]):
            raise ValueError("Unrecognized validation metadata.")
        family, operation = rule[:2]
        args = rule[2] if len(rule) > 2 and rule[2] is not None else []
        message = rule[3] if len(rule) > 3 and rule[3] is not None else ""
        if not isinstance(args, list) or not isinstance(message, str):
            raise ValueError("Invalid validation arguments or error text.")
        pattern = compiled = ""
        if family == 1 and kind == "text" and 1 <= operation <= 10:
            arity = 2 if operation in (7, 8) else 0 if operation in (9, 10) else 1
            if len(args) != arity or any(type(v) not in (str, int, float) for v in args):
                raise ValueError("Invalid number constraint arguments.")
            args = tuple(_number(str(v)) for v in args)
            if arity == 2 and args[0] > args[1]:
                raise ValueError("Number range starts above its end.")
            names = {1: ">", 2: ">=", 3: "<", 4: "<=", 5: "=", 6: "!="}
            description = (
                f"Number {names[operation]} {args[0]}"
                if operation <= 6
                else f"Number {'outside' if operation == 8 else 'between'} {args[0]} and {args[1]}"
                if operation <= 8
                else "Whole number"
                if operation == 10
                else "Finite number"
            )
        elif family == 2 and operation in (100, 101, 102):
            arity = 0 if operation == 102 else 1
            if len(args) != arity or any(not isinstance(v, str) for v in args):
                raise ValueError("Invalid text constraint arguments.")
            description = (
                "Valid email address"
                if operation == 102
                else f"{'Contains' if operation == 100 else 'Does not contain'} {args[0]!r}"
            )
        elif family == 6 and operation in (202, 203):
            if len(args) != 1 or type(args[0]) not in (str, int) or not re.fullmatch(r"[0-9]+", str(args[0])):
                raise ValueError("Invalid character-count constraint.")
            args = (int(args[0]),)
            description = f"{'At most' if operation == 202 else 'At least'} {args[0]} characters"
        elif family == 4 and operation in (299, 300, 301, 302):
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("Invalid regular expression arguments.")
            original = args[0]
            pattern = {
                299: original,
                300: f"^(?!^.*{original}.*$).*$",
                301: f"^{original}$",
                302: f"^(?!^{original}$).*$",
            }[operation]
            from .patterns import compile_pattern

            compiled = compile_pattern(pattern)
            verb = {
                299: "Contains pattern",
                300: "Does not contain pattern",
                301: "Matches pattern",
                302: "Does not match pattern",
            }[operation]
            description = f"{verb}: {original}"
        else:
            raise ValueError(f"Unsupported validation rule {family}/{operation}.")
        normalized.append(TextRule(family, operation, tuple(args), description, message, pattern, compiled))
    minimum = max((r.args[0] for r in normalized if r.family == 6 and r.operation == 203), default=0)
    maximum = min((r.args[0] for r in normalized if r.family == 6 and r.operation == 202), default=None)
    if maximum is not None and minimum > maximum:
        raise ValueError("Minimum character count exceeds maximum.")
    return tuple(normalized)


def validate_text(question, value):
    for rule in text_rules(question):
        op, args = rule.operation, rule.args
        if rule.family == 1:
            number = _number(value)
            if op <= 6:
                bound = args[0]
                accepted = {
                    1: number > bound,
                    2: number >= bound,
                    3: number < bound,
                    4: number <= bound,
                    5: number == bound,
                    6: number != bound,
                }[op]
            elif op in (7, 8):
                accepted = (args[0] <= number <= args[1]) == (op == 7)
            else:
                accepted = op == 9 or number == number.to_integral_value()
        elif rule.family == 2:
            accepted = (
                bool(re.fullmatch(EMAIL_PATTERN, value)) if op == 102 else ((args[0] in value) == (op == 100))
            )
        elif rule.family == 6:
            length = len(value.encode("utf-16-le", errors="surrogatepass")) // 2
            accepted = length <= args[0] if op == 202 else length >= args[0]
        else:
            from .patterns import matches

            accepted = matches(rule.compiled, value)
        if not accepted:
            raise ValueError(rule.message or rule.description)


def text_schema(question, required):
    """Prompt guidance; the shared validator remains authoritative for every rule."""
    result = {"type": "string", "minLength": int(required)}
    rules = text_rules(question)
    if rules:
        result["description"] = "; ".join(r.description for r in rules)
        if any(r.family == 6 for r in rules):
            result["description"] += ". Google counts UTF-16 units; an emoji may count as two characters."
        patterns = [
            r.pattern if r.family == 4 else "^" + _NUMBER + "$" if r.family == 1 else "" for r in rules
        ]
        if needs_email(question):
            patterns.append("^" + EMAIL_PATTERN + "$")
        patterns = [p for p in patterns if p]
        if len(patterns) == 1:
            result["pattern"] = patterns[0] if required else "(?:" + patterns[0] + ")|^$"
    return result


def time_settings(flags):
    if (
        not isinstance(flags, list)
        or len(flags) > 1
        or flags
        and flags[0] is not None
        and (type(flags[0]) not in (bool, int) or flags[0] not in (0, 1))
    ):
        raise ValueError("Unrecognized time settings.")
    return bool(flags and flags[0])


def time_format(question):
    return "HH:MM:SS (hours 00–72)" if question.qtype == "duration" else "HH:MM (24-hour clock)"


def time_pattern(question):
    return (
        r"(?:[0-6][0-9]|7[0-2]):[0-5][0-9]:[0-5][0-9]"
        if question.qtype == "duration"
        else r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    )


def parse_time(question, value, *, csv=False):
    if not isinstance(value, str):
        raise ValueError(f"Use {time_format(question)} for {question.text!r}.")
    if csv:
        value = value.strip()
    if not value:
        return ""
    if csv:
        match = re.fullmatch(r"\s*([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?\s*(AM|PM)?\s*", value, re.I)
        if match:
            hour, minute, second, period = match.groups()
            h, m, s = int(hour), int(minute), int(second or 0)
            if period:
                if question.qtype == "duration" or not 1 <= h <= 12:
                    raise ValueError(f"Invalid time for {question.text!r}: {value!r}.")
                h = h % 12 + (12 if period.upper() == "PM" else 0)
            if question.qtype == "time" and s:
                raise ValueError(f"Clock time does not accept nonzero seconds for {question.text!r}.")
            if question.qtype == "duration" and second is None:
                raise ValueError(f"Use HH:MM:SS for duration {question.text!r}.")
            value = f"{h:02d}:{m:02d}" + (f":{s:02d}" if question.qtype == "duration" else "")
    if not re.fullmatch(time_pattern(question), value):
        raise ValueError(f"Invalid time/duration; use {time_format(question)} for {question.text!r}.")
    return value


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
