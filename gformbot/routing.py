"""Compile section rules once and follow only a respondent's selected path."""

import random

SUPPORTED_TYPES = {"radio", "checkbox", "dropdown", "scale", "text", "textarea", "date", "time", "duration"}


class Routing:
    def __init__(self, form):
        count = len(form.sections) or max((q.page for q in form.questions), default=1)
        self.questions = {page: [] for page in range(1, count + 1)}
        self.linear = list(range(2, count + 1)) + [0]
        destinations = getattr(form, "section_next", [])
        if not isinstance(destinations, list):
            raise ValueError("Section destinations must be a list.")
        self.destinations = destinations or self.linear
        if (
            not isinstance(self.destinations, list)
            or len(self.destinations) != count
            or any(type(page) is not int or not 0 <= page <= count for page in self.destinations)
            or self.destinations[-1] != 0
        ):
            raise ValueError("Section destinations must name an existing section or Submit (0).")
        ids = getattr(form, "section_ids", [])
        if (
            not isinstance(ids, list)
            or ids
            and (
                len(ids) != count
                or any(not isinstance(value, str) or not value for value in ids)
                or len(set(ids)) != count
            )
        ):
            raise ValueError("Section identities must be distinct and match the section count.")
        self.enabled = self.destinations != self.linear
        for question in form.questions:
            if type(question.page) is not int or question.page not in self.questions:
                raise ValueError(f"Q{question.index}: section does not exist.")
            branches = getattr(question, "branches", {})
            if (
                not isinstance(branches, dict)
                or branches
                and (
                    question.qtype not in {"radio", "dropdown"}
                    or any(
                        option not in question.options or type(page) is not int or not 0 <= page <= count
                        for option, page in branches.items()
                    )
                )
            ):
                raise ValueError(f"Q{question.index}: invalid section routing choices or destinations.")
            self.enabled |= bool(branches)
            self.questions[question.page].append(question)
        scoped = {issue for q in form.questions for issue in q.unsupported}
        self.global_issues = [issue for issue in form.unsupported if issue not in scoped]
        self.blockers = {
            q.index: q.unsupported or [f"Unsupported control {q.qtype!r}: {q.text}"]
            for q in form.questions
            if q.unsupported or q.qtype not in SUPPORTED_TYPES
        }

    def require_supported(self, active=None):
        issues = self.global_issues + [
            issue
            for index, findings in self.blockers.items()
            if active is None or index in active
            for issue in findings
        ]
        if issues:
            raise ValueError("Unsupported form: cannot be filled reliably; " + "; ".join(issues))

    def choose_route(self, preset):
        """Find a supported terminating route without retries or enumerating profiles."""
        self.require_supported(())
        stack, seen = [(1, {})], set()
        while stack:
            page, chosen = stack.pop()
            if page == 0:
                return chosen
            if page in seen or any(q.index in self.blockers for q in self.questions[page]):
                continue
            seen.add(page)
            outcomes = {self.destinations[page - 1]: {}}
            for q in self.questions[page]:
                if not q.branches:
                    continue
                if q.index in preset:
                    values = [preset[q.index]]
                    if values[0] not in q.options and not (not q.required and values[0] == ""):
                        raise ValueError(f"Q{q.index}: choose an exact routing option.")
                else:
                    options = [
                        (opt, w)
                        for opt, w in zip(q.options, q.weights or [1] * len(q.options), strict=True)
                        if w > 0
                    ]
                    values = [
                        opt
                        for opt, _ in sorted(
                            options, key=lambda pair: random.random() ** (1 / pair[1]), reverse=True
                        )
                    ]
                    if not q.required:
                        values.append("")
                following = {}
                for value in values:
                    for target, answers in outcomes.items():
                        following.setdefault(q.branches.get(value, target), {**answers, q.index: value})
                outcomes = following
            if page == len(self.questions):
                outcomes = {0: next(iter(outcomes.values()), {})}
            for target, answers in reversed(list(outcomes.items())):
                if target not in seen:
                    stack.append((target, {**chosen, **answers}))
        raise ValueError("No supported terminating route matches the fixed answers and choice weights.")

    def next(self, page, answer):
        # Google ignores choice navigation on the final physical section.
        if page == len(self.questions):
            return 0
        target = self.destinations[page - 1]
        for question in self.questions[page]:
            if not question.branches:
                continue
            value = answer.get(question.index, answer.get(str(question.index), ""))
            if value is None or value == "":
                if question.required:
                    raise ValueError(
                        f"Q{question.index} ({question.text}): a required routing answer is blank."
                    )
                continue
            if not isinstance(value, str) or value not in question.options:
                raise ValueError(f"Q{question.index} ({question.text}): choose an exact routing option.")
            # The last answered routing question on a section wins. An explicit
            # Continue destination is physical next, not the section's default.
            target = question.branches.get(value, target)
        return target

    def walk(self, answer):
        page, visited = 1, set()
        while page:
            if page in visited:
                raise ValueError(
                    f"The answers return to section {page}; repeated sections cannot be filled reliably."
                )
            visited.add(page)
            yield page
            page = self.next(page, answer)

    def active(self, answer):
        return {q.index for page in self.walk(answer) for q in self.questions[page]}
