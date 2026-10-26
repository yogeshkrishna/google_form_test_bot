"""Compile section rules once and follow only a respondent's selected path."""


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
