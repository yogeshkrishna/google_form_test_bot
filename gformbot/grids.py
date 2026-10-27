"""Grid rows reuse choice answers; only identity and column rules are shared."""

import random


def parse_grid(item, offset, page):
    from .inspector import QuestionSchema, _get

    entries = item[4]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Missing grid rows for {item[1]!r}.")
    raw_rules = _get(item, 8)
    rules = [] if raw_rules is None else raw_rules
    if not isinstance(rules, list) or any(
        not isinstance(rule, list)
        or rule[:2] != [8, 205]
        or any(type(n) is not int for n in rule[:2])
        or any(rule[2:3])
        for rule in rules
    ):
        raise ValueError(f"Unknown grid validation for {item[1]!r}.")
    rows = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 4:
            raise ValueError(f"Unknown grid row for {item[1]!r}.")
        flags = _get(entry, 11)
        if flags is not None and (
            not isinstance(flags, list)
            or len(flags) > 1
            or flags
            and flags[0] is not None
            and (type(flags[0]) not in (int, bool) or flags[0] not in (0, 1))
        ):
            raise ValueError(f"Unknown grid settings for {item[1]!r}.")
        labels = entry[3]
        options = entry[1]
        if (
            not isinstance(labels, list)
            or len(labels) != 1
            or not isinstance(labels[0], str)
            or not labels[0].strip()
            or not isinstance(options, list)
            or not options
            or any(
                not isinstance(opt, list)
                or not opt
                or not isinstance(opt[0], str)
                or _get(opt, 2) is not None
                or _get(opt, 4) == 1
                for opt in options
            )
            or _get(entry, 4) not in (None, [])
            or type(entry[0]) is not int
            or entry[0] < 0
            or type(entry[2]) not in (int, bool)
            or entry[2] not in (0, 1)
        ):
            raise ValueError(f"Unknown grid row metadata for {item[1]!r}.")
        checkbox = bool(flags and flags[0])
        rows.append(
            QuestionSchema(
                offset + len(rows) + 1,
                f"{item[1]} [{labels[0]}]",
                "checkbox" if checkbox else "radio",
                bool(entry[2]),
                options=[opt[0] for opt in options],
                page=page,
                entry_id=str(entry[0]),
                item_id=str(item[0]),
                description=str(item[2] or ""),
                min_select=int(checkbox and bool(entry[2])),
                grid_title=str(item[1]),
                grid_row=labels[0],
                grid_unique=bool(rules),
            )
        )
    # Also rejects inconsistent modes/options before they can be advertised.
    from types import SimpleNamespace

    grid_groups(SimpleNamespace(questions=rows))
    return rows


def grid_groups(config):
    groups = {}
    for q in config.questions:
        if (
            not isinstance(q.grid_title, str)
            or not isinstance(q.grid_row, str)
            or type(q.grid_unique) is not bool
        ):
            raise ValueError(f"Q{q.index}: invalid grid settings.")
        if not q.grid_title:
            if q.grid_row or q.grid_unique:
                raise ValueError(f"Q{q.index}: grid row has no parent.")
            continue
        if not q.item_id or not q.entry_id or not q.grid_row or q.qtype not in ("radio", "checkbox"):
            raise ValueError(f"Q{q.index}: incomplete grid identity.")
        groups.setdefault(q.item_id, []).append(q)
    for rows in groups.values():
        first = rows[0]
        if (
            len({q.grid_row for q in rows}) != len(rows)
            or len({q.entry_id for q in rows}) != len(rows)
            or any(
                (q.grid_title, q.qtype, q.options, q.grid_unique, q.page)
                != (first.grid_title, first.qtype, first.options, first.grid_unique, first.page)
                for q in rows
            )
            or not first.options
            or len(set(first.options)) != len(first.options)
        ):
            raise ValueError(f"Inconsistent or ambiguous grid: {first.grid_title!r}.")
        if first.grid_unique and sum(q.required for q in rows) > len(first.options):
            raise ValueError(
                f"Impossible grid: {first.grid_title!r} has too few columns for its required rows."
            )
    return groups


def validate_columns(config, answer, active):
    used = {}
    for q in config.questions:
        if not q.grid_unique or q.index not in active:
            continue
        for option in answer[q.index] if q.qtype == "checkbox" else [answer[q.index]]:
            if not option:
                continue
            key = (q.item_id, option)
            if key in used:
                raise ValueError(
                    f"Grid {q.grid_title!r}: column {option!r} is selected in more than one row."
                )
            used[key] = q.index


def random_grid(rows, preset):
    """Match required rows to available columns, respecting fixed values and weights."""
    from .generator import parse_checkbox

    result = {
        q.index: parse_checkbox(q, preset[q.index]) if q.qtype == "checkbox" else preset[q.index]
        for q in rows
        if q.index in preset
    }
    reserved = set()
    for q in rows:
        if q.index in result:
            value = result[q.index]
            choices = value if isinstance(value, list) else [value]
            if any(option in reserved for option in choices if option):
                raise ValueError(f"Grid {q.grid_title!r}: fixed answers repeat a column.")
            reserved.update(option for option in choices if option)
    candidates = {}
    for q in rows:
        if q.index in result:
            continue
        options = [
            (opt, weight)
            for opt, weight in zip(q.options, q.weights or [1] * len(q.options), strict=True)
            if weight > 0 and opt not in reserved
        ]
        # Weighted random order, then an augmenting-path match instead of retries.
        candidates[q.index] = [
            opt for opt, _ in sorted(options, key=lambda pair: random.random() ** (1 / pair[1]), reverse=True)
        ]
    owners = {}

    def assign(index, seen):
        for option in candidates[index]:
            if option in seen:
                continue
            seen.add(option)
            if option not in owners or assign(owners[option], seen):
                owners[option] = index
                return True
        return False

    pending = [q for q in rows if q.index not in result]
    for q in sorted(pending, key=lambda q: not q.required):
        if not assign(q.index, set()) and q.required:
            raise ValueError(
                f"Grid {q.grid_title!r}: weights or fixed answers leave no valid column assignment."
            )
    for q in pending:
        selected = [option for option, index in owners.items() if index == q.index]
        result[q.index] = selected if q.qtype == "checkbox" else next(iter(selected), "")
    # Checkbox rows may take additional still-unused columns.
    if rows[0].qtype == "checkbox":
        for option in rows[0].options:
            eligible = [q for q in pending if option in candidates[q.index]]
            if option not in owners and option not in reserved and eligible and random.choice((False, True)):
                q = random.choice(eligible)
                result[q.index].append(option)
    return result
