"""Coordinate generation, durable progress, and review before producing a plan."""

from __future__ import annotations

import json
import uuid
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict

from . import ai
from .ai_draft import Draft
from .generation_contacts import ContactAnswerError
from .generation_quality import Quality
from .generator import validate_answer
from .routing import Routing

_REVIEW_BYTES = 28_000
_REPAIR_LIMIT = 2


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
        status=None,
        on_wait=None,
    ):
        self.config, self.reference, self.instructions = config, reference, instructions
        self.partials, self.draft = partials, draft
        self.progress, self.status = progress, status or (lambda _: None)
        self.on_wait = on_wait
        self.options = draft.options if draft else {"policy": policy or {}}
        self.state = deepcopy(draft.state) if draft else None
        self.review = deepcopy((self.state or {}).get("review", {"checked": [], "issues": []}))
        self.setup = deepcopy((self.state or {}).get("setup", {}))
        self.routes = Routing(config)
        self.questions = {q.index: q for q in config.questions}
        self.fixed = {q.index: q.fixed_value for q in config.questions if q.fixed_value is not None}
        self.repair_attempts = self.setup.setdefault("repair_attempts", {})
        self._last_group_progress = None
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
            for number, row in enumerate(self.partials or (), 1):
                self.quality.remember_contacts(row, number)
            for number, profile in enumerate((self.state or {}).get("profiles", ()), 1):
                self.quality.remember_contacts(profile["answers"], number)
        if evidence is not None:
            # Evidence changes the review inputs, not the accepted generation.
            self.review = {"checked": [], "issues": []}
            self.repair_attempts.clear()
        config._generation_draft = draft

    def _save(self, state=None):
        if state is not None:
            self.state = state
        self.state = {
            "strategy": self.state.get("strategy", "partition"),
            "profiles": self.state["profiles"],
            "review": self.review,
            "setup": self.setup,
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
            self.status(f"Generation draft saved: {self.draft.path}")
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
                    self.status(f"Choosing routes: {routed}/{self.config.count} saved")
                else:
                    self.status(f"Creating answers: {fields}/{expected} reached fields saved")

    def _protected(self, response):
        return {**self.fixed, **(self.partials[response - 1] if self.partials else {})}

    def _remember_row(self, response, row):
        saved = self.state["profiles"][response - 1]["answers"]
        for key in saved:
            if int(key) in row:
                saved[key] = deepcopy(row[int(key)])

    def _checks(self, rows):
        review_payloads, local_issues = {}, []
        changed = False
        for number, original in enumerate(rows, 1):
            route = list(self.routes.walk(original))
            protected = self._protected(number)
            revised = self.quality.apply(original, number, protected=protected, route=route)
            if list(self.routes.walk(revised)) != route:
                raise ValueError(f"Response {number}: a quality rule changed the selected form route.")
            if revised != original:
                revised = validate_answer(self.config, revised, routing=self.routes)
                ai._preserve_presets(revised, protected)
                rows[number - 1] = revised
                self._remember_row(number, revised)
                self.review["checked"] = [n for n in self.review["checked"] if n != number]
                changed = True
            issues = {
                **self.quality.issues(revised, route=route),
                **self.quality.evidence_issues(revised, route=route, protected=protected),
            }
            for question, reason in issues.items():
                local_issues.append(
                    {"response": number, "question": question, "reason": reason, "blocking": True}
                )
            if issues:
                continue
            targets = self.quality.review_keys(revised, route=route)
            if targets:
                active = self.routes.active(revised)
                questions = [self.questions[key] for key in targets]
                facts = self.quality.anchors(revised, questions)
                relevant = (
                    set(targets)
                    | {int(key) for key in self.quality.related_answers(revised, questions)}
                    | {q.index for q in self.config.questions if q.branches or q.other_branch is not None}
                )
                review_payloads[number] = {
                    "answers": {key: revised[key] for key in sorted(relevant & active) if key in revised},
                    "facts": facts,
                    "route": route,
                    "review_keys": list(targets),
                }
        if changed:
            self.review["issues"] = [
                issue for issue in self.review["issues"] if issue["response"] in self.review["checked"]
            ]
            self._save()
        if local_issues:
            affected = {issue["response"] for issue in local_issues}
            self.review["checked"] = [number for number in self.review["checked"] if number not in affected]
            self.review["issues"] = [
                issue for issue in self.review["issues"] if issue["response"] not in affected
            ] + local_issues
            self._save()
            issue = local_issues[0]
            raise ValueError(f"Response {issue['response']}, Q{issue['question']}: {issue['reason']}")
        return review_payloads

    def _review_context(self, group):
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
                    **({"options": self.questions[key].options} if self.questions[key].options else {}),
                }
                for key in sorted(keys)
            },
            "brief": self.quality.brief([self.questions[key] for key in sorted(keys)]),
        }

    def _repair(self, ask, rows, number, profile, issues):
        from .ai_partition import _slice

        protected = self._protected(number)
        keys = {
            item["question"]
            for item in issues
            if item.get("blocking", True)
            and item["question"] not in protected
            and not self.questions[item["question"]].branches
            and self.questions[item["question"]].other_branch is None
            and not self.questions[item["question"]].grid_title
        } - {int(key) for key in profile["facts"]}
        if not keys:
            return False
        old = rows[number - 1]
        questions = [q for q in self.config.questions if q.index in keys]
        subset = _slice(self.config, questions, 1, {q.page for q in questions})
        answer_keys = ", ".join(f'"{q.index}"' for q in questions)
        messages = _messages(
            "Correct the listed findings using the saved facts and exact form rules. "
            "Make the smallest correction that resolves the finding. Keep the person's voice, "
            "length and valid details where possible. Avoid new claims and filler. "
            f'Return a single JSON object with "profile_id": {number} and an "answers" object. '
            f"The only keys inside answers must be {answer_keys}. "
            "Return the corrected values, without commentary or code fences.",
            {
                **self._review_context({number: profile}),
                "profile_numbers": [number],
                "profile": profile,
                "repair": [item for item in issues if item["question"] in keys],
                "repair_questions": [asdict(q) for q in questions],
            },
            self.config.url,
        )
        if _payload_size(messages) > _REVIEW_BYTES:
            return False
        accepted, _, _ = ai._validate_records(subset, ask(messages), [number])
        if number not in accepted:
            return False
        revised = validate_answer(self.config, {**old, **accepted[number]}, routing=self.routes)
        ai._preserve_presets(revised, protected)
        if revised == old or self.quality.issues(revised, route=profile["route"]):
            return False
        try:
            self.quality.accept_contacts(revised, number, keys=keys, protected=protected)
        except ContactAnswerError:
            return False
        rows[number - 1] = revised
        self._remember_row(number, revised)
        profile["answers"].update(accepted[number])
        return True

    def _review(self, ask, rows, profiles):
        from .generation_review import build_review_request, parse_review_response

        def check(group, *, retry=False):
            failure = None
            context = ai._outbound_value(self._review_context(group), self.config.url)
            outbound = ai._outbound_value(group, self.config.url)
            try:
                reply = ask(build_review_request(context, outbound, retry=retry))
                reviewed = parse_review_response(reply, outbound)
            except (ValueError, OSError, KeyboardInterrupt) as exc:
                failure = exc
                reviewed = parse_review_response(getattr(exc, "partial_content", ""), outbound)
            if reviewed:
                self.review["checked"].extend(reviewed)
                self.review["issues"] = [
                    issue for issue in self.review["issues"] if issue["response"] not in reviewed
                ] + [
                    {
                        "response": number,
                        "question": issue["question"],
                        "reason": issue["reason"],
                        "blocking": issue["kind"] != "relevance",
                    }
                    for number, issues in reviewed.items()
                    for issue in issues
                ]
                self._save()
                self.status(f"Quality checked: {len(self.review['checked'])}/{len(profiles)} saved")
            if failure is not None:
                raise failure
            return reviewed

        while True:
            checked = set(self.review["checked"])
            pending = {number: profile for number, profile in profiles.items() if number not in checked}
            if pending:
                self.status(f"Checking linked answers for {len(pending)} response(s)…")
            while pending:
                group = {}
                for number, profile in pending.items():
                    candidate = {**group, number: profile}
                    context = self._review_context(candidate)
                    if _payload_size(build_review_request(context, candidate, retry=True)) > _REVIEW_BYTES:
                        if not group:
                            raise ValueError(f"Response {number}: quality review exceeds the prompt budget.")
                        break
                    group = candidate
                    if len(group) == 8:
                        break
                reviewed = check(group)
                missing = {number: profile for number, profile in group.items() if number not in reviewed}
                if missing:
                    corrected = check(missing, retry=True)
                    if len(corrected) != len(missing):
                        raise ValueError(
                            "Quality review returned incomplete results. Resume to check the remaining responses."
                        )
                for number in group:
                    del pending[number]
            blocked = {}
            for issue in self.review["issues"]:
                if issue.get("blocking", True):
                    blocked.setdefault(issue["response"], []).append(issue)
            if not blocked:
                return
            attempted = False
            for number, issues in blocked.items():
                key = str(number)
                if number not in profiles or self.repair_attempts.get(key, 0) >= _REPAIR_LIMIT:
                    continue
                attempted = True
                self.repair_attempts[key] = self.repair_attempts.get(key, 0) + 1
                if self._repair(ask, rows, number, profiles[number], issues):
                    self.review["checked"].remove(number)
                    self.review["issues"] = [
                        issue for issue in self.review["issues"] if issue["response"] != number
                    ]
                self._save()
            if not attempted:
                number, issues = next(iter(blocked.items()))
                raise ValueError(
                    f"Response {number}, Q{issues[0]['question']} needs review: {issues[0]['reason']}"
                )

    def run(self):
        from .chatgpt_http import Client

        try:
            with closing(Client(on_wait=self.on_wait)) as client:
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
                rows = (
                    ai.complete_responses(self.config, self.partials, **options)
                    if self.partials is not None
                    else ai.generate_responses(self.config, self.config.count, **options)
                )
                profiles = self._checks(rows)
                if profiles:
                    if self.setup.get("review_total") != len(profiles):
                        self.setup["review_total"] = len(profiles)
                        self._save()
                    self._review(lambda messages: client.ask(messages, timeout=180), rows, profiles)
                signals = self.quality.batch_signals(rows)
                if signals:
                    self.status("Batch quality signals: " + json.dumps(signals, ensure_ascii=False))
                if self.review["issues"]:
                    self.config._quality_warnings = deepcopy(self.review["issues"])
                    self.status(f"{len(self.review['issues'])} quality warning(s) saved with the plan.")
                    for issue in self.review["issues"][:3]:
                        self.status(f"Response {issue['response']}, Q{issue['question']}: {issue['reason']}")
                return rows
        except (ValueError, OSError, KeyboardInterrupt):
            if self.draft is not None:
                self.status("AI progress saved. Open Saved batches from Start to continue.")
                self.status(f'Resume with: gformbot prepare "{self.config.url}" --resume "{self.draft.path}"')
            raise
