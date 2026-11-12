"""Use form context, explicit facts and batch signals to improve generated answers."""

from __future__ import annotations

import random
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from threading import Lock

from .answers import copy_value
from .generation_contacts import ContactAnswerError, contact_identity, contact_kind, contact_problem
from .generator import CHOICE_TYPES, validate_answer
from .routing import Routing
from .validation import EMAIL_PATTERN, needs_email

_SPEC_KEYS = {
    "purpose",
    "style",
    "facts",
    "dependencies",
    "fields",
    "evidence",
    "allow_placeholders",
    "coverage",
}
_FIELD_KEYS = {"exact", "approved", "min_digits", "email_domain", "name_from", "route_values"}
_SOURCES = {"exact", "approved", "email_domain", "route_values"}
_CONTACT = re.compile(r"\b(?:e-?mail|telephone|phone|mobile(?: number)?)\b", re.I)
_QUANTITY = re.compile(r"\b(?:quantity|number of|how many)\b", re.I)
_ANCHOR = re.compile(
    r"\b(?:full name|first name|last name|name|e-?mail|age|occupation|role|country|city|"
    r"asset (?:type|category|state|status|tag|identifier)|lifecycle state|incident status|"
    r"(?:expected|reported|modeled) (?:[\w-]+ )?outcome|date of birth)\b",
    re.I,
)
_NARRATIVE = re.compile(
    r"\b(?:describe|explain|why|reason|summary|summari[sz]e|observation|findings|"
    r"what happened|how did|verify|confirm|readback|consistency|scenario)\b",
    re.I,
)
_CLAIM = re.compile(
    r"\b(?:readback|observations?|verification|verified|test results?|actual outcomes?|"
    r"(?:branch|route) [^.!?\n]{0,48}(?:visited|completed|switched)|"
    r"(?:submission|receipt) (?:confirmed|received))\b",
    re.I,
)
_MEDIA_DEPENDENCY = re.compile(
    r"\b(?:shown|depicted|displayed|visible|heard) (?:in|on) (?:the |this )?"
    r"(?:image|photo|picture|diagram|video|clip|audio|screenshot)\b|"
    r"\b(?:look at|watch|listen to|based on|refer to|observe|inspect|read|open|view) "
    r"(?:the |this )?(?:preceding |following |attached |embedded )?"
    r"(?:image|photo|picture|diagram|video|clip|audio|screenshot)\b|"
    r"\bwhich (?:image|photo|picture|video|clip|screenshot)\b|"
    r"\b(?:encoded|visible) in (?:the )?pixels\b|"
    r"\b(?:was|is) the (?:[\w-]+ ){0,3}(?:video|audio|clip) "
    r"(?:accessible|playable|available|audible)\b|"
    r"\b(?:describe|summari[sz]e|report) (?:the )?(?:visual and )?"
    r"(?:image|video|audio|visual) observations\b",
    re.I,
)
_PHONE = re.compile(r"\+?[\d(). \-]+")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_UNOBSERVED = {"not checked", "not observed", "not verified", "unknown"}


def _filled(value):
    return value not in (None, "", [], {})


def _read(row, key):
    return row.get(key, row.get(str(key)))


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
        raise ValueError(f"{label} must be nonempty text of at most 10000 characters.")
    return value


def _handle(name):
    if not isinstance(name, str):
        return ""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    return ".".join(re.findall(r"[a-z0-9]+", ascii_name))[:64].strip(".")


def _phone_digits(value):
    return (
        sum(char.isdecimal() for char in value) if isinstance(value, str) and _PHONE.fullmatch(value) else 0
    )


def _structured(question):
    return (
        bool(question.options or question.validation)
        or question.qtype in {"date", "time", "duration"}
        or question.qtype == "text"
        and _QUANTITY.search(question.text)
    )


class Quality:
    """Validate a form-specific quality policy and supply compact prompt context."""

    def __init__(self, config, spec=None, reference=None):
        self.config = config
        self.questions = {q.index: q for q in config.questions}
        self.routing = Routing(config)
        self.has_reference = bool(reference)
        if spec is None:
            spec = {}
        if not isinstance(spec, dict) or spec.keys() - _SPEC_KEYS:
            raise ValueError(
                "Quality settings must use purpose, style, facts, dependencies, fields, evidence, allow_placeholders or coverage."
            )
        self.purpose = _text(spec["purpose"], "Quality purpose") if "purpose" in spec else config.form_title
        self.style = _text(spec["style"], "Quality style") if "style" in spec else ""
        self.facts = set(self._keys(spec.get("facts", []), "Quality facts"))
        self.facts.update(
            q.index
            for q in config.questions
            if q.fixed_value is not None or q.branches or q.other_branch is not None or _ANCHOR.search(q.text)
        )
        self.dependencies = self._mapping(spec, "dependencies", self._dependency)
        self.evidence = self._mapping(spec, "evidence", lambda key, value: _text(value, f"Q{key} evidence"))
        self.placeholders = set(self._keys(spec.get("allow_placeholders", []), "Allowed placeholders"))
        self.fields = self._mapping(spec, "fields", self._field)
        self.contacts = {
            q.index: kind
            for q in config.questions
            if q.fixed_value is None
            and not self.fields.get(q.index, {}).keys() & _SOURCES
            and (kind := contact_kind(q))
        }
        self._contact_owners = {}
        self._contact_lock = Lock()
        self.facts.update(key for key, rule in self.fields.items() if rule.keys() & _SOURCES)
        self.coverage = self._keys(spec.get("coverage", []), "Quality coverage")
        if self.coverage and self.has_reference:
            raise ValueError(
                "Choose either planned coverage or a CSV guide, so guide relationships stay intact."
            )
        for key in self.coverage:
            q = self.questions[key]
            if (
                q.qtype not in CHOICE_TYPES - {"checkbox"}
                or not q.options
                or q.grid_title
                or q.index in self.routing.blockers
                or q.branches
                or q.other_branch is not None
                or self.routing.enabled
                and q.page != 1
                or q.fixed_value is not None
                or key in self.fields
            ):
                raise ValueError(f"Q{key} cannot be preselected for coverage on this form.")
        self.facts.update(self.coverage)
        self._media_keys = {
            q.index for q in config.questions if _MEDIA_DEPENDENCY.search(f"{q.text}\n{q.description}")
        }

    def _key(self, value):
        if type(value) is int and value in self.questions:
            return value
        if isinstance(value, str) and (match := re.fullmatch(r"[Qq]?([1-9][0-9]{0,8})", value)):
            key = int(match[1])
            if key in self.questions:
                return key
        raise ValueError(f"Quality settings reference an unknown question: {value!r}.")

    def _keys(self, values, label):
        if not isinstance(values, list):
            raise ValueError(f"{label} must be a list of question IDs.")
        keys = tuple(self._key(value) for value in values)
        if len(keys) != len(set(keys)):
            raise ValueError(f"{label} contains duplicate question IDs.")
        return keys

    def _mapping(self, spec, name, convert):
        values = spec.get(name, {})
        if not isinstance(values, dict):
            raise ValueError(f"Quality {name} must be keyed by question ID.")
        result = {}
        for raw, value in values.items():
            key = self._key(raw)
            if key in result:
                raise ValueError(f"Quality {name} contains duplicate Q{key}.")
            result[key] = convert(key, value)
        return result

    def _dependency(self, key, value):
        keys = self._keys(value, f"Q{key} dependencies")
        if not keys or key in keys:
            raise ValueError(f"Q{key} dependencies must name at least one other question.")
        return keys

    def remember_contacts(self, row, response_number):
        """Reserve previously accepted or supplied contacts without changing them."""
        with self._contact_lock:
            for key, kind in self.contacts.items():
                if identity := contact_identity(kind, _read(row, key)):
                    self._contact_owners.setdefault((key, identity), response_number)

    def accept_contacts(self, row, response_number, *, keys=None, protected=()):
        """Claim all new contacts together, even when generation workers overlap."""
        if not self.contacts:
            return
        claims, problems = [], {}
        for key, kind in self.contacts.items():
            if key in protected or (keys is not None and key not in keys):
                continue
            value = _read(row, key)
            if problem := contact_problem(kind, value):
                problems[key] = problem
            elif identity := contact_identity(kind, value):
                claims.append((key, identity, kind))
        if not (claims or problems):
            return
        with self._contact_lock:
            for key, identity, kind in claims:
                if self._contact_owners.get((key, identity), response_number) != response_number:
                    label = "email address" if kind == "email" else "phone number"
                    problems[key] = (
                        f"Use a distinct realistic {label} because another response used this one."
                    )
            if problems:
                raise ContactAnswerError(problems)
            self._contact_owners.update({(key, identity): response_number for key, identity, _ in claims})

    def _checked_value(self, key, value):
        try:
            return validate_answer(self.config, {key: value}, routing=self.routing, partial=True)[key]
        except ValueError as exc:
            raise ValueError(f"Invalid configured quality value for Q{key}: {exc}") from exc

    def _field(self, key, raw):
        if not isinstance(raw, dict) or not raw or raw.keys() - _FIELD_KEYS or len(raw.keys() & _SOURCES) > 1:
            raise ValueError(f"Q{key} needs supported quality rules and at most one source for its value.")
        question = self.questions[key]
        if question.branches or question.other_branch is not None:
            raise ValueError(f"Q{key} controls routing. Use a fixed or supplied routing answer instead.")
        result = dict(raw)
        if "min_digits" in result:
            minimum = result["min_digits"]
            if (
                question.qtype not in {"text", "textarea"}
                or type(minimum) is not int
                or not 1 <= minimum <= 32
                or "email_domain" in result
            ):
                raise ValueError(f"Q{key} needs a text phone field and a min_digits value from 1 to 32.")
        if "email_domain" in result:
            domain = result["email_domain"]
            if (
                question.qtype not in {"text", "textarea"}
                or not isinstance(domain, str)
                or len(domain) > 253
                or "." not in domain
                or "@" in domain
                or not re.fullmatch(EMAIL_PATTERN, f"test@{domain}")
            ):
                raise ValueError(f"Q{key} needs a valid test email domain.")
            if result.get("name_from") is None:
                raise ValueError(
                    f"Q{key} email_domain requires name_from. Set it to the question number "
                    "that supplies the respondent's name."
                )
            name_key = self._key(result["name_from"])
            if name_key == key or self.questions[name_key].qtype not in {"text", "textarea"}:
                raise ValueError(f"Q{key} name_from must name a different text question.")
            result["name_from"] = name_key
            result["email_domain"] = domain.lower()
        elif "name_from" in result:
            raise ValueError(f"Q{key} name_from needs an email_domain.")
        if "exact" in result:
            result["exact"] = self._checked_value(key, result["exact"])
        if "approved" in result:
            values = result["approved"]
            if not isinstance(values, list) or not values or len(values) > 500:
                raise ValueError(f"Q{key} approved values must be a list of 1 to 500 answers.")
            result["approved"] = [self._checked_value(key, value) for value in values]
            if any(not _filled(value) for value in result["approved"]):
                raise ValueError(f"Q{key} approved values must be nonblank.")
        if "route_values" in result:
            values = result["route_values"]
            if not isinstance(values, dict) or not values:
                raise ValueError(f"Q{key} route_values need at least one route.")
            mapped = {}
            for route, value in values.items():
                if not isinstance(route, str) or not re.fullmatch(r"1(?:,[1-9][0-9]{0,8})*", route):
                    raise ValueError(f"Q{key} route_values keys must be section numbers such as 1,3,4.")
                pages = tuple(map(int, route.split(",")))
                if (
                    len(set(pages)) != len(pages)
                    or any(p not in self.routing.questions for p in pages)
                    or (question.page not in pages)
                ):
                    raise ValueError(f"Q{key} route_values includes an invalid route.")
                mapped[pages] = self._checked_value(key, value)
            result["route_values"] = mapped
        if "min_digits" in result:
            values = ([result["exact"]] if "exact" in result else result.get("approved", [])) + list(
                result.get("route_values", {}).values()
            )
            if any(_phone_digits(value) < result["min_digits"] for value in values):
                raise ValueError(f"Q{key} configured phone values do not meet min_digits.")
        return result

    def _path(self, row, route):
        return tuple(route) or tuple(self.routing.walk(row))

    def _active(self, route):
        return {q.index for page in route for q in self.routing.questions[page]}

    def _expected(self, rule, row, route):
        if "exact" in rule:
            return rule["exact"]
        if "route_values" in rule:
            return rule["route_values"].get(route)
        if "email_domain" in rule and (handle := _handle(_read(row, rule["name_from"]))):
            return f"{handle}@{rule['email_domain']}"
        return None

    @staticmethod
    def _check_number(response_number):
        if type(response_number) is not int or response_number < 1:
            raise ValueError("Quality generation needs a positive response number.")

    def _derived(self, rule, row, response_number, route):
        if "approved" in rule:
            return rule["approved"][(response_number - 1) % len(rule["approved"])]
        return self._expected(rule, row, route)

    def presets(self, row, response_number, questions, *, route=()):
        """Provide known exact facts before prose, without replacing any supplied key."""
        if not self.fields:
            return {}
        self._check_number(response_number)
        values = {int(key): value for key, value in row.items()}
        keys = {self._key(q if isinstance(q, (int, str)) else q.index) for q in questions}
        path = tuple(route)
        active = (
            self._active(path)
            if path
            else {q.index for q in self.questions.values() if not self.routing.enabled or q.page == 1}
        )
        pending = [
            (key, rule)
            for key, rule in self.fields.items()
            if key in keys and key in active and key not in values and self.questions[key].fixed_value is None
        ]
        derived = {}
        for key, rule in sorted(pending, key=lambda item: "email_domain" in item[1]):
            value = self._derived(rule, values, response_number, path)
            if value is not None:
                value = self._checked_value(key, value)
                derived[key] = values[key] = value
        return derived

    def coverage_partials(self, count, partials, seed):
        """Plan eligible choices across the run while retaining supplied row values."""
        if not self.coverage:
            return partials
        if type(count) is not int or not 1 <= count <= self.config.max_runs:
            raise ValueError("Invalid response count for planned coverage.")
        if partials is not None and len(partials) != count:
            raise ValueError("Planned coverage needs one partial row per response.")
        rows = [dict(partials[i]) if partials is not None else {} for i in range(count)]
        rng = random.Random(seed)
        for key in self.coverage:
            q = self.questions[key]
            weighted = [
                (option, weight)
                for option, weight in zip(q.options, q.weights or [1] * len(q.options), strict=True)
                if weight > 0
            ]
            if not weighted:
                raise ValueError(f"Q{key} needs a positive option weight for planned coverage.")
            rng.shuffle(weighted)
            maximum = max(weight for _, weight in weighted)
            scale = count / sum(weight / maximum for _, weight in weighted)
            deficit = {option: weight / maximum * scale for option, weight in weighted}
            open_rows = []
            for row in rows:
                if key in row or str(key) in row:
                    value = _read(row, key)
                    if isinstance(value, str) and value in deficit:
                        deficit[value] -= 1
                else:
                    open_rows.append(row)
            rng.shuffle(open_rows)
            for row in open_rows:
                option = max(deficit, key=deficit.__getitem__)
                row[key] = option
                deficit[option] -= 1
        return rows

    def anchors(self, row, questions=()):
        keys = self.facts.copy()
        for question in questions:
            keys.update(self.dependencies.get(question.index, ()))
            name_key = self.fields.get(question.index, {}).get("name_from")
            if name_key is not None:
                keys.add(name_key)
        return {str(key): copy_value(value) for key in sorted(keys) if _filled(value := _read(row, key))}

    def related_answers(self, row, questions):
        """Keep persona and explicit dependencies, plus nearby structured answers on short forms."""
        answers = self.anchors(row, questions)
        if len(self.questions) < 24:
            pages = {self.questions[q.index].page for q in questions}
            for key, question in self.questions.items():
                if question.page in pages and key not in self.contacts and _structured(question):
                    if _filled(value := _read(row, key)):
                        answers[str(key)] = copy_value(value)
        return answers

    def apply(self, row, response_number, *, protected=(), route=()):
        """Derive values for configured fields without replacing protected answers."""
        values = {int(key): copy_value(value) for key, value in row.items()}
        if not self.fields:
            return values
        self._check_number(response_number)
        active_route = self._path(values, route)
        active = self._active(active_route)
        fixed = {int(key) for key in protected}
        fixed.update(q.index for q in self.questions.values() if q.fixed_value is not None)
        eligible = [
            (key, rule)
            for key, rule in self.fields.items()
            if key in values and key in active and key not in fixed
        ]
        # An exact/approved source can supply the name used to derive an email.
        for key, rule in sorted(eligible, key=lambda item: "email_domain" in item[1]):
            value = self._derived(rule, values, response_number, active_route)
            if value is not None:
                values[key] = copy_value(value)
        return values

    def issues(self, row, *, route=(), keys=None):
        """Return enforceable field failures, independently of the model."""
        if not self.fields:
            return {}
        path = self._path(row, route)
        active = self._active(path)
        if keys is not None:
            active &= {self._key(key) for key in keys}
        failures = {}
        for key, rule in self.fields.items():
            if key not in active:
                continue
            value = _read(row, key)
            expected = self._expected(rule, row, path)
            if "route_values" in rule and expected is None:
                failures[key] = "No configured value matches this response's route."
            elif "email_domain" in rule and expected is None:
                failures[key] = f"A name-based email requires a usable name in Q{rule['name_from']}."
            elif expected is not None and expected != value:
                failures[key] = (
                    "Answer conflicts with its configured exact, name-based or route-derived value."
                )
            elif "approved" in rule and value not in rule["approved"]:
                failures[key] = "Answer is outside the configured approved values."
            elif "min_digits" in rule and _phone_digits(value) < rule["min_digits"]:
                failures[key] = f"Enter a phone value with at least {rule['min_digits']} digits."
        return failures

    def evidence_issues(self, row, *, route=(), protected=()):
        provided = {int(key) for key in protected}
        provided.update(q.index for q in self.questions.values() if q.fixed_value is not None)
        provided.update(key for key, rule in self.fields.items() if "exact" in rule or "approved" in rule)
        missing = self._media_keys - self.evidence.keys() - self.placeholders - provided
        if not missing:
            return {}
        return {
            key: "Answer depends on media. Supply the relevant fact or explicitly allow a test placeholder."
            for key in sorted(missing & self._active(self._path(row, route)))
            if self.questions[key].required or _filled(_read(row, key))
            if not (
                isinstance(answer := _read(row, key), str)
                and answer in self.questions[key].options
                and answer.strip().casefold() in _UNOBSERVED
            )
        }

    def review_keys(self, row, *, route=()):
        """Select narrative or dependency-bearing answers for focused semantic review."""
        path = self._path(row, route)
        active = self._active(path)
        complex_form = len(active) >= 24 or len(path) >= 4
        structured_pages = {
            q.page
            for key, q in self.questions.items()
            if key in active
            and key not in self._media_keys
            and key not in self.contacts
            and _structured(q)
            and _filled(_read(row, key))
        }
        return tuple(
            q.index
            for q in self.questions.values()
            if q.index in active
            and _filled(_read(row, q.index))
            and (
                q.index in self.dependencies
                or q.index in self._media_keys
                or q.index in self.placeholders
                or q.index in self.evidence
                or _CLAIM.search(f"{q.text}\n{q.description}")
                or (
                    (complex_form or q.page in structured_pages)
                    and (
                        q.qtype == "textarea"
                        or q.qtype == "text"
                        and _NARRATIVE.search(f"{q.text}\n{q.description}")
                    )
                )
            )
        )

    def brief(self, questions, accepted=()):
        questions = tuple(questions)
        keys = {q.index for q in questions}
        instructions = [
            "Write as a plausible respondent to this specific form. Match its purpose and the tone implied by the questions.",
            "Settle each person's relevant choices and facts before writing linked answers. "
            "Use a useful amount of detail and natural variation. "
            "Leave optional answers blank when the respondent has nothing relevant to add.",
            "Keep internal profile IDs out of answers unless a field explicitly calls for them.",
            "Report only evidence supplied for this response. Do not invent actions taken, media observations, or test outcomes.",
        ]
        if self.has_reference:
            instructions.append(
                "Follow relationships and style in the guide; retain plausible repeats without copying whole examples."
            )
        if self.style:
            instructions.append(self.style)
        if any(self.contacts.get(key) == "email" for key in keys):
            instructions.append(
                "Use a distinct believable address for each generated email. For personal email, choose a "
                "normal provider such as Gmail, Outlook or Yahoo and a natural name-based handle when a "
                "name is available. Vary initials and name combinations; use a short numeric suffix only "
                "where natural. For work or school, follow the form's context. Avoid placeholder domains "
                "and generated IDs in the handle."
            )
        if any(self.contacts.get(key) == "phone" for key in keys):
            instructions.append(
                "Use a distinct plausible phone number for each generated contact. Match the respondent's "
                "location, using the form, user instructions and reference data as context. Use that location's "
                "usual area or mobile prefixes and national length. Use international format with a country "
                "calling code when the form accepts it, and local format when requested. Where the form allows "
                "respondents from different places, use plausible locations without assuming a default country. "
                "Avoid dummy numbers, repeated digits and sequential endings across responses."
            )
        for key in sorted(keys & self.placeholders):
            instructions.append(
                f"Q{key} may use a test placeholder. Do not describe it as a verified observation."
            )
        result = {"purpose": self.purpose, "writing_guidance": instructions}
        evidence = {str(key): value for key, value in self.evidence.items() if key in keys}
        if evidence:
            result["supplied_evidence"] = evidence
        dependencies = {
            str(key): [str(source) for source in value]
            for key, value in self.dependencies.items()
            if key in keys
        }
        if dependencies:
            result["answer_dependencies"] = dependencies
        fields = {}
        for key, rule in self.fields.items():
            if key not in keys:
                continue
            guidance = []
            if "exact" in rule:
                guidance.append("Use the configured exact answer.")
            if "approved" in rule:
                guidance.append("Use an approved test value assigned by the application.")
            if "email_domain" in rule:
                guidance.append(
                    f"Derive the email handle from Q{rule['name_from']} and use @{rule['email_domain']}."
                )
            if "route_values" in rule:
                guidance.append("Use the answer determined by this profile's actual route.")
            if "min_digits" in rule:
                guidance.append(
                    f"Use a naturally formatted phone value with at least {rule['min_digits']} digits."
                )
            fields[str(key)] = {
                "instructions": guidance,
                **({"exact": copy_value(rule["exact"])} if "exact" in rule else {}),
            }
        if fields:
            result["quality_field_rules"] = fields
        signals = self.batch_signals(accepted, questions=questions)
        if signals:
            result["batch_signals"] = signals
            result["writing_guidance"].append(
                "Vary the flagged patterns where appropriate. Preserve natural repetition and reference distributions."
            )
        return result

    def batch_signals(self, rows, *, questions=None):
        """Summarize concentration and repetition without copying earlier answers."""
        if isinstance(rows, Mapping):
            rows = [row for _, row in sorted(rows.items(), key=lambda pair: int(pair[0]))]
        rows = list(rows)
        if len(rows) < 2:
            return {}
        repeats, contacts, cycles = [], [], []
        for q in self.questions.values() if questions is None else questions:
            values = [_read(row, q.index) for row in rows]
            rule = self.fields.get(q.index, {})
            if needs_email(q) or _CONTACT.search(q.text) or "email_domain" in rule or "min_digits" in rule:
                values = [
                    v.casefold().strip() if "@" in v else "".join(c for c in v if c.isdecimal())
                    for v in values
                    if isinstance(v, str) and v.strip()
                ]
                duplicates = sum(count - 1 for value, count in Counter(values).items() if value)
                if duplicates:
                    contacts.append({"question": q.index, "repeated_answers": duplicates})
            elif q.qtype in CHOICE_TYPES:
                cycle = self._choice_cycle(values)
                if cycle:
                    cycles.append({"question": q.index, **cycle})
            elif q.qtype in {"text", "textarea"}:
                signatures = [
                    re.sub(r"\d+", "#", " ".join(_WORDS.findall(value.casefold())))
                    for value in values
                    if isinstance(value, str) and len(value) >= 40
                ]
                repeated = sum(count - 1 for signature, count in Counter(signatures).items() if signature)
                if repeated:
                    repeats.append({"question": q.index, "repeated_templates": repeated})
        return {
            key: values[:8]
            for key, values in (
                ("choice_cycles", cycles),
                ("repeated_contacts", contacts),
                ("repeated_text", repeats),
            )
            if values
        }

    @staticmethod
    def _choice_cycle(values):
        normalized = [
            tuple(sorted(value)) if isinstance(value, list) else value if isinstance(value, str) else ""
            for value in values
        ]
        counts = Counter(value for value in normalized if value)
        if len(counts) < 2 or len(values) < 24:
            return None
        total = sum(counts.values())
        baseline = sum((count / total) ** 2 for count in counts.values())
        best = None
        for offset in range(2, min(21, len(values) // 2 + 1)):
            pairs = [
                (a, b) for a, b in zip(normalized[:-offset], normalized[offset:], strict=True) if a and b
            ]
            if len(pairs) < 12:
                continue
            rate = sum(a == b for a, b in pairs) / len(pairs)
            if rate >= 0.65 and rate >= baseline + 0.2 and (best is None or rate > best[1]):
                best = (offset, rate)
        if best:
            return {
                "row_interval": best[0],
                "match_rate": round(best[1], 3),
                "baseline_rate": round(baseline, 3),
            }
        return None
