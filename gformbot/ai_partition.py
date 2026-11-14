"""Generate large forms in route-aware question groups, then validate complete profiles."""

from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from threading import RLock

from .answers import copy_value
from .generator import validate_answer
from .routing import Routing

_GROUP_BYTES = 12_000


def _slice(config, questions, count, pages):
    """Keep original answer IDs and constraints, with local linear navigation."""
    return replace(
        config,
        questions=[replace(q, page=1, branches={}, other_branch=None) for q in questions],
        count=count,
        sections=[" / ".join(config.sections[p - 1] for p in sorted(pages) if p <= len(config.sections))],
        section_ids=[],
        section_next=[],
        unsupported=[],
        instructions=[{**note, "page": 1} for note in config.instructions if note["page"] in pages],
        media=[],
        pool_file=None,
        pool_column_map={},
    )


def _units(config):
    """Split large sections between controls, keeping every grid atomic."""
    groups, seen = defaultdict(list), set()
    for question in config.questions:
        if question.grid_title:
            identity = (question.page, question.item_id or question.grid_title)
            if identity in seen:
                continue
            seen.add(identity)
            questions = [
                q
                for q in config.questions
                if q.grid_title and (q.page, q.item_id or q.grid_title) == identity
            ]
        else:
            questions = [question]
        size = len(json.dumps([asdict(q) for q in questions], ensure_ascii=False).encode())
        groups[question.page].append((questions, size))
    return groups


def _restore(config, state, bases, routes, units):
    """Accept only validated atomic prefixes of a supported route."""
    if not isinstance(state, dict) or set(state) != {"profiles"}:
        raise ValueError("Invalid AI draft progress.")
    profiles = state["profiles"]
    if not isinstance(profiles, list) or len(profiles) != len(bases):
        raise ValueError("AI draft response count does not match.")
    allowed = {str(q.index) for q in config.questions}
    rows, positions, paths = [], [], []
    for record, base in zip(profiles, bases, strict=True):
        if not isinstance(record, dict) or set(record) != {"answers", "position", "route"}:
            raise ValueError("Invalid AI draft response progress.")
        values, position, path = record["answers"], record["position"], record["route"]
        if not isinstance(values, dict) or any(key not in allowed for key in values):
            raise ValueError("AI draft contains unknown question keys.")
        if type(position) is not int or position < 0:
            raise ValueError("Invalid AI draft question position.")
        row = {int(key): copy_value(value) for key, value in values.items()}
        if any(key not in row or row[key] != value for key, value in base.items()):
            raise ValueError("AI draft changed a preselected answer.")
        if path is None:
            if position or row != base:
                raise ValueError("Unrouted AI draft contains generated answers.")
        else:
            if not isinstance(path, list) or any(type(page) is not int for page in path):
                raise ValueError("Invalid AI draft section route.")
            actual_path = tuple(routes.walk(row))
            if tuple(path) != actual_path:
                raise ValueError("AI draft route does not match its answers.")
            active = routes.active(row)
            routes.require_supported(active)
            task = [unit for page in actual_path for unit in units[page]]
            if position > len(task):
                raise ValueError("AI draft position exceeds its selected route.")
            prefix = {q.index for questions, _ in task[:position] for q in questions}
            navigation = {
                q.index
                for q in config.questions
                if q.index in active and (q.branches or q.other_branch is not None)
            }
            validated = prefix | navigation
            complete_map = position == len(task) and len(row) == len(allowed)
            if not complete_map and (not validated <= row.keys() or row.keys() - (validated | base.keys())):
                raise ValueError("AI draft answers do not match its completed question groups.")
            if complete_map:
                if validate_answer(config, row, routing=routes) != row:
                    raise ValueError("AI draft contains answers that require normalization.")
            elif validated:
                questions = [q for q in config.questions if q.index in validated]
                subset = _slice(config, questions, 1, {q.page for q in questions})
                supplied = {key: row[key] for key in validated}
                if validate_answer(subset, supplied) != supplied:
                    raise ValueError("AI draft contains answers that require normalization.")
            # JSON writers may sort object keys. Rebuild acceptance order so the
            # bounded recent-answer context remains identical after resuming.
            order = dict.fromkeys(
                [
                    *base,
                    *(q.index for q in config.questions if q.index in navigation),
                    *(q.index for questions, _ in task[:position] for q in questions),
                    *row,
                ]
            )
            row = {key: row[key] for key in order}
            path = actual_path
        rows.append(row)
        positions.append(position)
        paths.append(path)
    return rows, positions, paths


def generate_partitioned(
    config,
    count,
    *,
    reference=None,
    instructions="",
    timeout=180,
    batch_size=20,
    max_retries=2,
    progress=None,
    partials=None,
    on_wait=None,
    resume_state=None,
    checkpoint=None,
    quality=None,
    client=None,
):
    from .ai import GenerationError, _generate_responses, _preserve_presets
    from .chatgpt_http import Client

    routes = Routing(config)
    fixed = {q.index: q.fixed_value for q in config.questions if q.fixed_value is not None}
    rows = [
        {key: copy_value(value) for key, value in {**(partials[i] if partials else {}), **fixed}.items()}
        for i in range(count)
    ]
    completed = {}
    positions = [0] * count
    paths = [None] * count
    units = _units(config)
    if resume_state is not None:
        rows, positions, paths = _restore(config, resume_state, rows, routes, units)
    revision, saved_revision, reported = 0, -1, 0
    own_client = client is None
    client = client if client is not None else Client(on_wait=on_wait)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gformbot-ai")
    state_lock = RLock()

    def save_progress():
        nonlocal saved_revision, reported
        if checkpoint is None or saved_revision == revision:
            return
        checkpoint(
            {
                "profiles": [
                    {
                        "answers": {str(key): copy_value(value) for key, value in row.items()},
                        "position": positions[identity],
                        "route": list(paths[identity]) if paths[identity] is not None else None,
                    }
                    for identity, row in enumerate(rows)
                ]
            }
        )
        saved_revision = revision
        if progress and len(completed) > reported:
            reported = len(completed)
            progress(reported, count)

    def generate_part(subconfig, identities, references, context, shared_context, received):
        keys = {q.index for q in subconfig.questions}
        presets = []
        for index in identities:
            selected = {key: value for key, value in rows[index].items() if key in keys}
            if quality and paths[index] is not None:
                selected.update(
                    quality.presets(
                        rows[index],
                        index + 1,
                        subconfig.questions,
                        route=paths[index],
                    )
                )
            presets.append(selected)

        def qualify(identity, value):
            index = identities[identity - 1]
            if not quality or paths[index] is None:
                return value
            candidate = {**rows[index], **value}
            revised = quality.apply(candidate, index + 1, protected=presets[identity - 1], route=paths[index])
            scoped = keys - {
                key
                for key, rule in quality.fields.items()
                if "name_from" in rule and rule["name_from"] not in candidate
            }
            issues = quality.issues(revised, route=paths[index], keys=scoped)
            if issues:
                key, reason = next(iter(issues.items()))
                raise ValueError(f"Q{key}: {reason}")
            validated = validate_answer(subconfig, {key: revised[key] for key in keys})
            quality.accept_contacts(
                {**candidate, **validated}, index + 1, keys=keys, protected=presets[identity - 1]
            )
            return validated

        return _generate_responses(
            replace(subconfig, count=len(identities)),
            len(identities),
            reference=references,
            instructions=instructions,
            timeout=timeout,
            batch_size=batch_size,
            max_retries=max_retries,
            partials=presets,
            _partition=False,
            _profile_context=context,
            _context=shared_context,
            _client=client,
            quality=quality,
            _on_reply=received,
            _transform=qualify if quality else None,
        )

    def generate(subconfig, identities, *, on_record, references=None, context=None, shared_context=None):
        if context is None:
            context = [{"response_number": identity + 1} for identity in identities]
        from .ai import _estimate_profile_size
        from .ai_context import pack_history

        shared, profiles = pack_history(
            shared_context or {}, {str(i + 1): item for i, item in enumerate(context)}
        )
        request_size = (
            len(
                json.dumps(
                    [asdict(subconfig), shared, profiles, references, instructions], ensure_ascii=False
                ).encode()
            )
            + 4_500
        )
        needs_batches = (
            len(identities) > batch_size
            or len(identities) * _estimate_profile_size(subconfig, instructions, references or []) > 24_000
            or request_size > 28_000
        )

        delivered = set()

        def receive(part_ids, records):
            with state_lock:
                try:
                    for local_index, value in records.items():
                        if type(local_index) is not int or not 0 <= local_index < len(part_ids):
                            raise GenerationError("A generated slice has an invalid response identity.")
                        identity = part_ids[local_index]
                        if identity not in delivered:
                            on_record(identity, value)
                            delivered.add(identity)
                finally:
                    save_progress()

        if len(identities) <= 4 or not needs_batches:
            try:
                values = generate_part(
                    subconfig,
                    identities,
                    references,
                    context,
                    shared_context,
                    lambda records: receive(identities, records),
                )
            except GenerationError as error:
                receive(identities, getattr(error, "partial_records", {}))
                raise
            receive(identities, dict(enumerate(values)))
            if len(values) != len(identities):
                raise GenerationError("A generated group is missing response records.")
            return
        middle = (len(identities) + 1) // 2
        offsets = (0, middle, len(identities))
        futures = {
            executor.submit(
                generate_part,
                subconfig,
                identities[start:end],
                references,
                context[start:end],
                shared_context,
                lambda records, part=identities[start:end]: receive(part, records),
            ): (start, end)
            for start, end in zip(offsets[:-1], offsets[1:], strict=True)
        }
        consumed = set()

        def consume(future):
            consumed.add(future)
            start, end = futures[future]
            try:
                result = future.result()
            except GenerationError as error:
                receive(identities[start:end], getattr(error, "partial_records", {}))
                raise
            receive(identities[start:end], dict(enumerate(result)))
            if len(result) != end - start:
                raise GenerationError("A generated group is missing response records.")

        try:
            for future in as_completed(futures):
                consume(future)
        except BaseException:
            client.cancel()
            for future in futures:
                future.cancel()
            for future in futures.keys() - consumed:
                if not future.cancelled():
                    try:
                        consume(future)
                    except Exception:
                        pass  # Retain recoverable rows without replacing the original failure.
            raise

    try:
        save_progress()
        routing_questions = [q for q in config.questions if q.branches or q.other_branch is not None]
        pending = [identity for identity, path in enumerate(paths) if path is None]
        if routing_questions and pending:
            route_config = _slice(config, routing_questions, count, set(range(1, len(routes.questions) + 1)))
            route_config.questions = [replace(q, required=False) for q in route_config.questions]
            route_config.form_description += (
                "\nChoose a supported terminating route. Routing metadata: "
                + json.dumps(
                    {
                        "start": 1,
                        "submit": 0,
                        "section_next": routes.destinations,
                        "section_titles": config.sections,
                        "question_sections": {str(q.index): q.page for q in routing_questions},
                        "choices": {str(q.index): q.branches for q in routing_questions},
                        "other_choices": {
                            str(q.index): q.other_branch
                            for q in routing_questions
                            if q.other_branch is not None
                        },
                        "blocked_sections": sorted(
                            {q.page for q in config.questions if q.index in routes.blockers}
                        ),
                        "required_routing_questions": [q.index for q in routing_questions if q.required],
                        "rule": (
                            "The last answered routing question on each section wins. "
                            "The final physical section always ends. Never revisit a section."
                        ),
                    },
                    ensure_ascii=False,
                )
            )

            def accept_route(identity, value):
                nonlocal revision
                candidate = {**rows[identity], **value}
                try:
                    reached = routes.active(candidate)
                    routes.require_supported(reached)
                except ValueError:
                    return
                rows[identity].update({key: item for key, item in value.items() if key in reached})
                paths[identity] = tuple(routes.walk(candidate))
                revision += 1

            for attempt in range(max_retries + 1):
                generate(
                    route_config,
                    pending,
                    references=[
                        {q.index: row.get(q.index, "") for q in routing_questions}
                        for row in (reference or [])
                    ],
                    on_record=accept_route,
                )
                invalid = [identity for identity in pending if paths[identity] is None]
                if not invalid:
                    break
                if attempt == max_retries:
                    raise GenerationError("ChatGPT could not select supported terminating routes.")
                pending = invalid
                route_config.form_description += (
                    "\nPrior routing choices formed a loop or entered an unsupported section. Correct them."
                )

        paths = [
            path if path is not None else tuple(routes.walk(row))
            for path, row in zip(paths, rows, strict=True)
        ]
        revision += 1
        save_progress()
        tasks = [[unit for page in path for unit in units[page]] for path in paths]
        reference_active = [routes.active(row) for row in (reference or [])]

        def finish(identity):
            if identity in completed or positions[identity] != len(tasks[identity]):
                return
            answer = validate_answer(config, rows[identity], routing=routes)
            _preserve_presets(answer, fixed, partials[identity] if partials else None)
            completed[identity] = answer
            if progress and checkpoint is None:
                progress(len(completed), count)

        for identity in range(count):
            finish(identity)

        while len(completed) < count:
            waiting = defaultdict(list)
            for identity, position in enumerate(positions):
                if identity in completed:
                    continue
                if position == len(tasks[identity]):
                    finish(identity)
                else:
                    questions, _ = tasks[identity][position]
                    waiting[tuple(q.index for q in questions)].append(identity)
            if not waiting:
                break
            # Let other branches catch up before generating their shared tail.
            # Individually terminating routes can still form a cyclic union;
            # in that case keep making progress with the largest ready group.
            later = {
                tuple(q.index for q in unit[0])
                for identity, task in enumerate(tasks)
                for unit in task[positions[identity] + 1 :]
            }
            ready = [identities for key, identities in waiting.items() if key not in later]
            identities = max(ready or waiting.values(), key=len)
            questions, total, steps = [], 0, 0
            while True:
                next_units = [
                    tasks[i][positions[i] + steps] if positions[i] + steps < len(tasks[i]) else None
                    for i in identities
                ]
                unit = next_units[0]
                if unit is None or any(other is None or other[0] != unit[0] for other in next_units[1:]):
                    break
                group, size = unit
                if questions and total + size > _GROUP_BYTES:
                    break
                questions.extend(group)
                total += size
                steps += 1
            pages = {q.page for q in questions}
            subset = _slice(config, questions, len(identities), pages)
            keys = {q.index for q in questions}
            guide = [
                {key: value for key, value in row.items() if key in keys}
                for row, reached in zip(reference or [], reference_active, strict=True)
                if keys <= reached
            ]
            from .ai_context import history_keys, selected_history

            relevant = history_keys(config, questions, instructions)
            context = [
                {
                    "response_number": i + 1,
                    "earlier_answers": selected_history(
                        rows[i], relevant, presets={**(partials[i] if partials else {}), **fixed}
                    ),
                    "route_sections": list(paths[i]),
                    **({"profile_facts": quality.anchors(rows[i], questions)} if quality else {}),
                }
                for i in identities
            ]
            known = {int(key) for profile in context for key in profile["earlier_answers"]}
            shared = {
                "previous_questions": {str(q.index): q.text for q in config.questions if q.index in known}
            }
            if config.sections and len(config.sections) > 1:
                reached = {page for i in identities for page in paths[i]}
                shared.update(
                    original_question_sections={str(q.index): q.page for q in questions},
                    route_section_titles={
                        page: title for page, title in enumerate(config.sections, 1) if page in reached
                    },
                    route_scope="Interpret prior-branch questions at their original section. Separate gates do not indicate switching a branch.",
                )

            def accept_group(identity, value, *, group=subset, group_keys=keys, advance=steps):
                nonlocal revision
                value = validate_answer(group, value)
                _preserve_presets(
                    value,
                    {key: item for key, item in fixed.items() if key in group_keys},
                    {key: item for key, item in rows[identity].items() if key in group_keys},
                )
                rows[identity].update(value)
                positions[identity] += advance
                revision += 1
                finish(identity)

            generate(
                subset,
                identities,
                references=guide,
                context=context,
                shared_context=shared,
                on_record=accept_group,
            )
        return [completed[i] for i in range(count)]
    except (GenerationError, ValueError, OSError) as error:
        raise GenerationError(
            str(error),
            completed=len(completed),
            requested=count,
            partial_responses=[completed[i] for i in sorted(completed)],
            partial_records=dict(completed),
            partial_groups={
                identity: {
                    "answers": {key: copy_value(value) for key, value in row.items()},
                    "position": positions[identity],
                    "route": paths[identity],
                }
                for identity, row in enumerate(rows)
                if identity not in completed and row
            },
            provider_error=getattr(error, "provider_error", None),
        ) from error
    except KeyboardInterrupt:
        client.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        if own_client:
            client.close()
        save_progress()
