"""Coordinate generation, durable progress, and review before producing a plan."""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from contextlib import closing, nullcontext
from copy import deepcopy
from dataclasses import asdict

from . import ai
from .ai_draft import Draft
from .generation_quality import Quality
from .generator import validate_answer
from .routing import Routing

_REVIEW_BYTES = 28_000
_REVIEW_REPLY_BYTES = 6_400
_REVIEW_ROW_LIMIT = 20
_INITIAL_REVIEW_FIELD_BYTES = 144
_MISSING_REVIEW_FIELD_BYTES = 280
_REPAIR_LIMIT = 2


class ReviewBlockedError(ValueError):
    """Review cannot continue until the user changes the inputs."""


def _payload_size(messages):
    return len(json.dumps("\n".join(message["content"] for message in messages), ensure_ascii=False).encode())


def _messages(instruction, payload, url):
    return [
        {"role": "system", "content": instruction},
        {
            "role": "user",
            "content": json.dumps(
                ai._outbound_value(payload, url), ensure_ascii=False, separators=(",", ":")
            ),
        },
    ]


def _sized_groups(profiles, build, reply_cost, *, max_rows=None):
    group, estimated_reply = {}, 0
    for number, profile in profiles.items():
        cost = reply_cost(profile)
        candidate = {**group, number: profile}
        if group and (
            (max_rows is not None and len(group) >= max_rows)
            or estimated_reply + cost > _REVIEW_REPLY_BYTES
            or _payload_size(build(candidate)) > _REVIEW_BYTES
        ):
            yield group
            candidate = {number: profile}
            estimated_reply = 0
        group = candidate
        estimated_reply += cost
    if group:
        yield group


class Generation:
    """Own accepted local work, independently of the provider's conversation."""

    def __init__(
        self,
        config,
        reference,
        instructions,
        *,
        partials=None,
        draft=None,
        policy=None,
        evidence=None,
        progress=None,
        stage_progress=None,
        phase_progress=None,
        status=None,
        on_wait=None,
    ):
        self.config, self.reference, self.instructions = config, reference, instructions
        self.partials, self.draft = partials, draft
        self.progress, self.status = progress, status or (lambda _: None)
        self.stage_progress = stage_progress
        self.phase_progress = phase_progress or (lambda _: None)
        self.on_wait = on_wait
        self.options = draft.options if draft else {"policy": policy or {}}
        self.state = deepcopy(draft.state) if draft else None
        self.review = deepcopy((self.state or {}).get("review", {"checked": [], "issues": []}))
        self.setup = deepcopy((self.state or {}).get("setup", {}))
        self.review_related = self.setup.pop("review_related", {})
        self.routes = Routing(config)
        self.questions = {q.index: q for q in config.questions}
        self.fixed = {q.index: q.fixed_value for q in config.questions if q.fixed_value is not None}
        self.repair_attempts = self.setup.setdefault("repair_attempts", {})
        # An explicit resume gives exhausted rows one fresh budget. This is consumed once per run.
        self._retry_exhausted = draft is not None
        self._last_group_progress = None
        self._timing = None
        spec = deepcopy(self.options.get("policy") or {})
        override = self.setup.get("evidence", {})
        if evidence is not None:
            if not isinstance(evidence, dict) or set(evidence) - {"evidence", "allow_placeholders"}:
                raise ValueError("A resumed draft accepts only evidence and allow_placeholders in --quality.")
            additions = Quality(config, evidence)
            override = {
                "evidence": {
                    **override.get("evidence", {}),
                    **{str(k): v for k, v in additions.evidence.items()},
                },
                "allow_placeholders": sorted(
                    set(override.get("allow_placeholders", [])) | additions.placeholders
                ),
            }
            self.setup["evidence"] = override
        if override:
            base = Quality(config, spec, reference)
            spec["evidence"] = {**{str(k): v for k, v in base.evidence.items()}, **override["evidence"]}
            spec["allow_placeholders"] = sorted(base.placeholders | set(override["allow_placeholders"]))
        self.quality = Quality(config, spec, reference)
        if draft is None:
            planned = self.quality.coverage_partials(config.count, self.partials, seed=uuid.uuid4().int)
            prepared = []
            for number in range(1, config.count + 1):
                row = planned[number - 1] if planned else {}
                prepared.append(
                    {
                        **row,
                        **self.quality.presets({**self.fixed, **row}, number, config.questions),
                    }
                )
            if planned is not None or any(prepared):
                self.partials = prepared
        if self.quality.contacts:
            if draft is None and not self.quality.has_reference:
                self.setup["synthetic_contact_history"] = True
            for number, row in enumerate(self.partials or (), 1):
                self.quality.remember_contacts(row, number)
            for number, profile in enumerate((self.state or {}).get("profiles", ()), 1):
                self.quality.remember_contacts(
                    profile["answers"],
                    number,
                    synthetic=self.setup.get("synthetic_contact_history") is True,
                    protected=self._protected(number),
                )
        if evidence is not None:
            # Evidence changes the review inputs, not the accepted generation.
            self.review = {"checked": [], "issues": []}
            self.review_related.clear()
            self.repair_attempts.clear()
        config._generation_draft = draft

    def _save(self, state=None):
        if state is not None:
            self.state = state
        self.state = {
            "strategy": self.state.get("strategy", "partition"),
            "profiles": self.state["profiles"],
            "review": self.review,
            "setup": {
                **self.setup,
                **({"review_related": self.review_related} if self.review_related else {}),
            },
        }
        if self.draft is None:
            self.draft = Draft.create(
                self.config,
                self.reference,
                self.instructions,
                self.partials,
                self.state,
                options=self.options,
            )
            self.config._generation_draft = self.draft
            self.status("Draft created. You can continue from Continue batches if generation stops.")
        else:
            self.draft.save(self.state)
        if state is not None and self.state["strategy"] == "partition":
            profiles = self.state["profiles"]
            routed = sum(profile["route"] is not None for profile in profiles)
            fields, expected = 0, 0
            for profile in profiles:
                if profile["route"] is None:
                    continue
                active = {str(q.index) for page in profile["route"] for q in self.routes.questions[page]}
                expected += len(active)
                fields += len(active.intersection(profile["answers"]))
            marker = (routed, fields, expected)
            if marker != self._last_group_progress and routed:
                self._last_group_progress = marker
                if routed < self.config.count:
                    self._report_progress("routes", routed, self.config.count, "Choosing routes")
                else:
                    self._report_progress(
                        "fields", fields, expected, "Creating answers", unit=" reached fields"
                    )

    def _report_progress(self, stage, saved, total, label, *, unit=""):
        if self.stage_progress:
            self.stage_progress(stage, saved, total)
        else:
            self.status(f"{label}: {saved}/{total}{unit} saved")

    def _protected(self, response):
        return {**self.fixed, **(self.partials[response - 1] if self.partials else {})}

    def _remember_row(self, response, row):
        saved = self.state["profiles"][response - 1]["answers"]
        for key in saved:
            if int(key) in row:
                saved[key] = deepcopy(row[int(key)])

    def _clear_review(self, responses):
        responses = set(responses)
        self.review["checked"] = [number for number in self.review["checked"] if number not in responses]
        self.review["issues"] = [
            issue for issue in self.review["issues"] if issue["response"] not in responses
        ]
        for number in responses:
            self.review_related.pop(str(number), None)

    def _checks(self, rows):
        review_payloads, local_issues = {}, []
        invalidated = set()
        routing_keys = {q.index for q in self.config.questions if q.branches or q.other_branch is not None}
        for number, original in enumerate(rows, 1):
            route = list(self.routes.walk(original))
            protected = self._protected(number)
            revised = self.quality.apply(original, number, protected=protected, route=route)
            if revised != original:
                if list(self.routes.walk(revised)) != route:
                    raise ValueError(f"Response {number}: a quality rule changed the selected form route.")
                revised = validate_answer(self.config, revised, routing=self.routes)
                ai._preserve_presets(revised, protected)
                rows[number - 1] = revised
                self._remember_row(number, revised)
                invalidated.add(number)
            issues = {
                **self.quality.issues(revised, route=route),
                **self.quality.evidence_issues(revised, route=route, protected=protected),
            }
            for question, reason in issues.items():
                local_issues.append(
                    {"response": number, "question": question, "reason": reason, "blocking": True}
                )
            if issues:
                invalidated.add(number)
                continue
            targets = self.quality.review_keys(revised, route=route)
            if targets:
                active = {q.index for page in route for q in self.routes.questions[page]}
                questions = [self.questions[key] for key in targets]
                facts = self.quality.anchors(revised, questions)
                relevant = (
                    set(targets)
                    | {int(key) for key in self.quality.related_answers(revised, questions)}
                    | routing_keys
                )
                review_payloads[number] = {
                    "answers": {key: revised[key] for key in sorted(relevant & active) if key in revised},
                    "facts": facts,
                    "route": route,
                    "review_keys": list(targets),
                }
        if invalidated:
            self._clear_review(invalidated)
            self.review["issues"].extend(local_issues)
            self._save()
        if local_issues:
            issue = local_issues[0]
            raise ReviewBlockedError(f"Response {issue['response']}, Q{issue['question']}: {issue['reason']}")
        return review_payloads

    def _review_context(self, group, *, include_options=True):
        keys = {int(key) for profile in group.values() for key in profile["answers"]}
        pages = {self.questions[key].page for key in keys}
        reached = {page for profile in group.values() for page in profile["route"]}
        return {
            "title": self.config.form_title,
            "description": self.config.form_description,
            "instructions": self.instructions,
            "route_sections": {
                page: title for page, title in enumerate(self.config.sections, 1) if page in reached
            },
            "section_instructions": [note for note in self.config.instructions if note["page"] in pages],
            "questions": {
                key: {
                    "text": self.questions[key].text,
                    "description": self.questions[key].description,
                    "page": self.questions[key].page,
                    **(
                        {"options": self.questions[key].options}
                        if include_options and self.questions[key].options
                        else {}
                    ),
                }
                for key in sorted(keys)
            },
            "brief": self.quality.brief([self.questions[key] for key in sorted(keys)]),
        }

    def _repair_keys(self, number, profile, issues):
        protected = self._protected(number)
        keys = {
            item["question"]
            for item in issues
            if item.get("blocking", True)
            and item["question"] in profile["review_keys"]
            and item["question"] not in protected
            and not self.questions[item["question"]].branches
            and self.questions[item["question"]].other_branch is None
            and not self.questions[item["question"]].grid_title
        } - {int(key) for key in profile["facts"]}
        return tuple(sorted(keys))

    def _repair_request(self, group, questions, findings, *, compact=False):
        numbers = list(group)
        answer_keys = ", ".join(f'"{q.index}"' for q in questions)
        keys = {q.index for q in questions}
        repairs = {
            number: [item for item in findings[number] if item["question"] in keys] for number in group
        }
        if len(group) == 1:
            number = numbers[0]
            output = f'Return a single JSON object with "profile_id": {number} and an "answers" object. '
            profiles = {"profile": group[number], "repair": repairs[number]}
        else:
            output = (
                'Return one JSON object per line with the integer "profile_id" and an "answers" object. '
                "Include exactly one record for each listed profile ID. Keep different profiles independent. "
            )
            profiles = {"profiles": group, "repair": repairs}
        return _messages(
            "Correct the listed findings using the saved facts and exact form rules. "
            "Make the smallest correction that resolves the finding. Keep the person's voice, "
            "length and valid details where possible. Keep each corrected answer filled. Avoid new claims and filler. "
            f"{output}"
            f"The only keys inside answers must be {answer_keys}. "
            "Return the corrected values, without commentary or code fences.",
            {
                **self._review_context(group, include_options=not compact),
                "profile_numbers": numbers,
                **profiles,
                "repair_questions": [asdict(q) for q in questions],
            },
            self.config.url,
        )

    def _repair_group(self, ask, rows, group, questions, messages):
        from .ai_partition import _slice

        subset = _slice(self.config, questions, len(group), {q.page for q in questions})
        keys = {q.index for q in questions}
        failure = None
        try:
            content = ask(messages)
        except (ValueError, OSError, KeyboardInterrupt) as exc:
            failure = exc
            content = getattr(exc, "partial_content", "")
        accepted, _, _ = ai._validate_records(subset, content, group)
        repaired = set()
        for number, changes in accepted.items():
            profile = group[number]
            old, protected = rows[number - 1], self._protected(number)
            route = profile["route"]
            try:
                revised = validate_answer(self.config, {**old, **changes}, routing=self.routes)
                ai._preserve_presets(revised, protected)
                if (
                    revised == old
                    or any(not revised[key] for key in keys)
                    or list(self.routes.walk(revised)) != route
                    or self.quality.issues(revised, route=route)
                    or self.quality.evidence_issues(revised, route=route, protected=protected)
                ):
                    continue
                self.quality.accept_contacts(revised, number, keys=keys, protected=protected)
            except ValueError:
                continue
            rows[number - 1] = revised
            self._remember_row(number, revised)
            profile["answers"].update({key: revised[key] for key in keys})
            repaired.add(number)
        attempted = group if failure is None else repaired
        for number in attempted:
            key = str(number)
            self.repair_attempts[key] = self.repair_attempts.get(key, 0) + 1
        if repaired:
            self._clear_review(repaired)
        if attempted:
            self._save()
            if repaired and self.stage_progress:
                self.stage_progress("review", len(self.review["checked"]), self.setup["review_total"])
        if failure is not None:
            raise failure

    def _remember_review(self, reviewed):
        self._clear_review(reviewed)
        self.review["checked"].extend(reviewed)
        for number, issues in reviewed.items():
            related = defaultdict(set)
            for issue in issues:
                question = issue["question"]
                blocking = issue["kind"] != "relevance"
                self.review["issues"].append(
                    {
                        "response": number,
                        "question": question,
                        "reason": issue["reason"],
                        "blocking": blocking,
                    }
                )
                if blocking:
                    related[str(question)].update(
                        item["question"] for item in issue["evidence"] if item["question"] != question
                    )
            references = {key: sorted(indices) for key, indices in related.items() if indices}
            if references:
                self.review_related[str(number)] = references

    def _review(self, ask, rows, profiles):
        from .generation_review import build_review_request, parse_review_response

        def request(group, *, retry, include_options):
            context = ai._outbound_value(
                self._review_context(group, include_options=include_options), self.config.url
            )
            outbound = ai._outbound_value(group, self.config.url)
            return build_review_request(context, outbound, retry=retry), outbound

        def check(group, *, retry=False, include_options=True):
            failure = None
            messages, outbound = request(group, retry=retry, include_options=include_options)
            try:
                reply = ask(messages)
                reviewed = parse_review_response(reply, outbound)
            except (ValueError, OSError, KeyboardInterrupt) as exc:
                failure = exc
                reviewed = parse_review_response(getattr(exc, "partial_content", ""), outbound)
            if reviewed:
                self._remember_review(reviewed)
                self._save()
                self._report_progress("review", len(self.review["checked"]), len(profiles), "Quality checked")
            if failure is not None:
                raise failure
            return reviewed, len(reply.encode("utf-8")) if isinstance(reply, str) else 0

        def review_prompt(group, *, include_options=True):
            return request(group, retry=True, include_options=include_options)[0]

        def review_pending(pending):
            field_bytes = _INITIAL_REVIEW_FIELD_BYTES
            max_rows = 8
            failure = None

            def review_cost(profile):
                return 64 + field_bytes * min(8, len(profile["review_keys"]))

            while pending:
                group = next(_sized_groups(pending, review_prompt, review_cost, max_rows=max_rows))
                for number in group:
                    del pending[number]
                # A singleton cannot be split further. Its selected options are already
                # validated and supplied as answers, so omit the full choice lists if needed.
                include_options = len(group) > 1 or _payload_size(review_prompt(group)) <= _REVIEW_BYTES
                if (
                    not include_options
                    and _payload_size(review_prompt(group, include_options=False)) > _REVIEW_BYTES
                ):
                    number = next(iter(group))
                    failure = failure or ReviewBlockedError(
                        f"Response {number}: quality review exceeds the prompt budget."
                    )
                    continue
                reviewed, reply_bytes = check(group, include_options=include_options)
                missing = {number: profile for number, profile in group.items() if number not in reviewed}
                if not missing:
                    # Complete replies are often just one short record per response. Allow room
                    # for findings on the next group, and never grow by more than twofold.
                    fields = sum(min(8, len(profile["review_keys"])) for profile in group.values())
                    field_bytes = max(32, (2 * reply_bytes - 64 * len(group)) / max(1, fields))
                    max_rows = min(_REVIEW_ROW_LIMIT, 2 * len(group))
                    continue
                # Estimate larger replies and use fewer rows after an incomplete response.
                field_bytes = max(_MISSING_REVIEW_FIELD_BYTES, field_bytes)
                max_rows = max(1, len(group) // 2)
                items = list(missing.items())
                half = (len(items) + 1) // 2
                corrected, _ = check(dict(items[:half]), retry=True, include_options=include_options)
                if items[half:]:
                    more, _ = check(dict(items[half:]), retry=True, include_options=include_options)
                    corrected.update(more)
                if len(corrected) != len(missing):
                    number = min(missing.keys() - corrected.keys())
                    failure = failure or ValueError(
                        f"Quality review returned incomplete results for response {number}. "
                        "Resume to check the remaining responses."
                    )
            # Review groups are independent. Preserve completed checks before pausing the batch.
            if failure is not None:
                raise failure

        while True:
            checked = set(self.review["checked"])
            pending = {number: profile for number, profile in profiles.items() if number not in checked}
            if pending:
                self.phase_progress("review")
                if self.stage_progress is None:
                    self.status(f"Checking linked answers for {len(pending)} response(s)…")
                review_pending(pending)

            blocked = defaultdict(list)
            for issue in self.review["issues"]:
                if issue.get("blocking", True):
                    blocked[issue["response"]].append(issue)
            if not blocked:
                return
            with self._phase("repair"):
                attempted, retryable_issue = self._repair_blocked(ask, rows, profiles, blocked)
            if not attempted:
                number = retryable_issue if retryable_issue is not None else next(iter(blocked))
                issues = blocked[number]
                error = ValueError if retryable_issue is not None else ReviewBlockedError
                raise error(
                    f"Response {number}, Q{issues[0]['question']} needs review: {issues[0]['reason']}"
                )

    def _repair_blocked(self, ask, rows, profiles, blocked):
        compatible = defaultdict(dict)
        retryable_issue = None
        retry_exhausted, self._retry_exhausted = self._retry_exhausted, False
        for number, issues in blocked.items():
            profile = profiles.get(number)
            if profile is None:
                continue
            keys = self._repair_keys(number, profile, issues)
            if not keys:
                continue
            key = str(number)
            attempts = self.repair_attempts.get(key, 0)
            if attempts >= _REPAIR_LIMIT:
                if not retry_exhausted:
                    retryable_issue = number if retryable_issue is None else retryable_issue
                    continue
                self.repair_attempts.pop(key)
                attempts = 0
            # A second attempt runs separately so one difficult profile cannot crowd out another.
            compatible[(tuple(profile["route"]), keys, number if attempts else None)][number] = profile

        attempted = False
        for (_, keys, _), route_profiles in compatible.items():
            questions = [self.questions[key] for key in keys]

            def build(group, questions=questions):
                return self._repair_request(group, questions, blocked)

            def reply_cost(profile, keys=keys):
                values = {key: profile["answers"].get(key) for key in keys}
                return 96 + 2 * len(json.dumps(values, ensure_ascii=False).encode())

            for group in _sized_groups(route_profiles, build, reply_cost):
                messages = build(group)
                if _payload_size(messages) > _REVIEW_BYTES:
                    focused = {}
                    for number, profile in group.items():
                        relevant = set(keys)
                        relevant.update(
                            int(key) for key in self.quality.related_answers(rows[number - 1], questions)
                        )
                        references = self.review_related.get(str(number), {})
                        for key in keys:
                            relevant.update(references.get(str(key), ()))
                        focused[number] = {
                            "route": profile["route"],
                            "facts": self.quality.anchors(rows[number - 1], questions),
                            "answers": {
                                key: rows[number - 1][key]
                                for key in sorted(relevant)
                                if key in rows[number - 1]
                            },
                        }
                    messages = self._repair_request(focused, questions, blocked, compact=True)
                if _payload_size(messages) <= _REVIEW_BYTES:
                    self.phase_progress("repair")
                    self._repair_group(ask, rows, group, questions, messages)
                    attempted = True
        return attempted, retryable_issue

    def _phase(self, name):
        return self._timing.phase(name) if self._timing is not None else nullcontext()

    def run(self):
        from .chatgpt_http import Client

        client_options = {"on_wait": self.on_wait}
        if os.environ.get("GFORMBOT_AI_TIMING") == "1":
            from .generation_timing import GenerationTiming

            self._timing = GenerationTiming()
            client_options["timing"] = self._timing
        completed = False
        try:
            with closing(Client(**client_options)) as client:
                options = {
                    "reference": self.reference,
                    "instructions": self.instructions,
                    "on_wait": self.on_wait,
                    "resume_state": self.state,
                    "checkpoint": self._save,
                    "progress": self.progress,
                    "quality": self.quality,
                    "_client": client,
                }
                with self._phase("generate"):
                    if self.partials is None:
                        rows = ai.generate_responses(self.config, self.config.count, **options)
                    else:
                        rows = ai.complete_responses(self.config, self.partials, **options)
                if self.stage_progress:
                    self.stage_progress("answers", len(rows), self.config.count)
                self.phase_progress("validate")
                with self._phase("validate"):
                    profiles = self._checks(rows)
                if profiles:
                    self.phase_progress("review")
                    with self._phase("review"):
                        if self.setup.get("review_total") != len(profiles):
                            self.setup["review_total"] = len(profiles)
                            self._save()
                        if self.stage_progress:
                            self.stage_progress("review", len(self.review["checked"]), len(profiles))
                        self._review(lambda messages: client.ask(messages, timeout=180), rows, profiles)
                warnings = self.review["issues"]
                signals = self.quality.batch_signals(rows)
                if signals:
                    self.status("Batch quality signals: " + json.dumps(signals, ensure_ascii=False))
                if warnings:
                    self.config._quality_warnings = deepcopy(warnings)
                    self.status(f"{len(warnings)} quality warning(s) saved with the plan.")
                    for issue in warnings[:3]:
                        self.status(f"Response {issue['response']}, Q{issue['question']}: {issue['reason']}")
                completed = True
                return rows
        finally:
            if self._timing is not None and (path := self._timing.save(completed=completed)):
                self.status(f"AI timing saved: {path}")
