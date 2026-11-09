"""A short guided workflow with the same operations available as CLI commands."""

from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import (
    DEFAULT_CONFIG_NAME,
    RunConfig,
    clean_path,
    config_from_dict,
    config_from_schema,
    config_to_dict,
    load_config,
    save_config,
    validate_config,
)
from .terminal import (
    PromptBack,
    accent_choice,
    interactive_input,
    prompt_steps,
    run_prompt,
    wizard_navigation,
)

console = Console(highlight=False, safe_box=True)
_INQUIRER = interactive_input() and interactive_input(sys.stdout)


@dataclass
class MenuChoice:
    value: object
    name: str


def _choice(value, name):
    return MenuChoice(value, name)


def _ask_text(message, default="", *, completed=False):
    if _INQUIRER:
        from InquirerPy import get_style, inquirer

        options = {"instruction": f"(Enter for {default})"} if default else {}
        if completed:
            options["style"] = get_style(
                dict.fromkeys(("answermark", "answered_question", "answer"), "#9299a6"),
                style_override=False,
            )
            options["transformer"] = lambda value: value.strip() or default or "(skipped)"
        value = run_prompt(inquirer.text(message=message, **options), typing=True)
        if value is None:
            raise click.Abort()
        return value.strip() or default
    value = input(f"{message}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def _ask_confirm(message, default=False, *, completed=False):
    if _INQUIRER:
        from InquirerPy import get_style, inquirer

        options = {}
        if completed:
            options["style"] = get_style(
                dict.fromkeys(("answermark", "answered_question", "answer"), "#9299a6"),
                style_override=False,
            )
        value = run_prompt(inquirer.confirm(message=message, default=default, **options))
        if value is None:
            raise click.Abort()
        return value
    while True:
        value = input(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "n", "no"}:
            return value in {"y", "yes"}
        console.print("Enter yes or no.")


def _ask_select(message, choices, default=None):
    if _INQUIRER:
        from InquirerPy import inquirer

        prompt = inquirer.select(
            message=message, choices=[{"value": c.value, "name": c.name} for c in choices], default=default
        )
        if message == "Start" and not console.no_color:
            accent_choice(prompt, "quick")
        value = run_prompt(prompt)
        if value is None:
            raise click.Abort()
        return value
    console.print(Text(message))
    for index, choice in enumerate(choices, 1):
        style = "bold magenta" if message == "Start" and choice.value == "quick" else ""
        console.print(
            Text(f"  {index}. {choice.name}" + (" (default)" if choice.value == default else ""), style=style)
        )
    while True:
        value = input("Enter number: ").strip()
        if not value and default is not None:
            return default
        if value.isdecimal() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1].value
        console.print(f"Enter a number between 1 and {len(choices)}.")


def _ask_number(message, default=1, min_val=1, max_val=500, *, completed=False):
    while True:
        options = {"completed": True} if completed else {}
        value = _ask_text(message, str(default), **options)
        if value.isdecimal() and min_val <= int(value) <= max_val:
            return int(value)
        console.print(f"Enter a whole number between {min_val} and {max_val}.")


def _print_banner():
    console.print(
        Panel.fit(
            f"[bold cyan]gformbot[/bold cyan] [dim]{__version__}[/dim]\nPrepare and submit Google Forms.",
            border_style="cyan",
        )
    )


def _section(title):
    console.rule(Text(title, style="bold cyan"))


def _info(message):
    console.print(Text(str(message), style="cyan"))


def _clipboard_url():
    try:
        import pyperclip

        value = pyperclip.paste().strip()
        valid, _ = _validate_form_url(value)
        return value if valid else None
    except Exception:
        return None


def _validate_form_url(url):
    from .inspector import clean_url

    try:
        clean_url(url)
        return True, None
    except ValueError as exc:
        return False, str(exc)


def _url_prompt(*, completed=False):
    url = _clipboard_url()
    options = {"completed": True} if completed else {}
    while True:
        if url and _ask_confirm(f"Use the form URL in your clipboard? {url}", default=True, **options):
            return url
        try:
            while True:
                entered = _ask_text("Paste your public Google Form link", **options)
                valid, message = _validate_form_url(entered)
                if valid:
                    return entered
                console.print(Text(message, style="yellow"))
        except PromptBack:
            if not url:
                raise


def _show_form(config):
    from .validation import date_format, text_rules, time_format

    def destination(page):
        return "Submit" if page == 0 else f"Section {page}: {config.sections[page - 1]}"

    _section(config.form_title)
    if config.form_description:
        console.print(Text(config.form_description))
    table = Table(show_lines=True, header_style="bold cyan", expand=True)
    table.add_column("Q", style="dim", no_wrap=True)
    table.add_column("Question / description", ratio=3)
    table.add_column("Type / required", ratio=1)
    table.add_column("Choices / rules", ratio=3)
    previous_page = None
    for q in config.questions:
        notes = [note for note in getattr(config, "instructions", []) if note["before_question"] == q.index]

        def show_note(note):
            table.add_row(
                "",
                Text(note["title"] + ("\n" + note["description"] if note["description"] else "")),
                "Instructions",
                "",
            )

        for note in notes:
            if note["page"] != q.page:
                show_note(note)
        if q.page != previous_page:
            section = config.sections[q.page - 1] if q.page <= len(config.sections) else f"Section {q.page}"
            if config.section_next:
                section += " → " + destination(config.section_next[q.page - 1])
            table.add_row("", Text(section, style="bold cyan"), "", "")
            previous_page = q.page
        for note in notes:
            if note["page"] == q.page:
                show_note(note)
        details = "\n".join(
            option + (" → " + destination(q.branches[option]) if option in q.branches else "")
            for option in q.options
        )
        rules = []
        if q.min_select:
            rules.append(f"At least {q.min_select} selections")
        if q.max_select is not None:
            rules.append(f"At most {q.max_select} selections")
        if q.exclusive_options:
            rules.append("Choose alone: " + "; ".join(q.exclusive_options))
        if q.validation and q.qtype in ("text", "textarea"):
            try:
                rules.extend(rule.description for rule in text_rules(q))
            except ValueError:
                rules.append("Custom validation (not supported)")
        if q.qtype == "date":
            rules.append(date_format(q))
        if q.qtype in ("time", "duration"):
            rules.append(time_format(q))
        if q.qtype == "rating":
            rules.append("Icon: " + q.rating_icon)
        if q.grid_unique:
            rules.append("Each column can be used in only one row of this grid")
        if q.allow_other:
            rules.append('Other: use {"choices": [...], "other": "custom text"}')
        if rules:
            details += ("\n\n" if details else "") + "\n".join(rules)
        question = q.text + ("\n" + q.description if q.description else "")
        table.add_row(
            str(q.index),
            Text(question),
            Text(q.qtype + ("\nRequired" if q.required else "\nOptional")),
            Text(details or "Free text"),
        )
    console.print(table)
    if config.media:
        media_table = Table(title="Images and videos (in form order)")
        for label in ("Type", "Section", "Placement", "Caption"):
            media_table.add_column(label)
        for asset in config.media:
            media_table.add_row(
                Text(asset["kind"]),
                str(asset["page"]),
                asset["placement"],
                Text(asset["caption"]),
            )
        console.print(media_table)
    for note in getattr(config, "instructions", []):
        if note["before_question"] > len(config.questions):
            _info(note["title"] + ("\n" + note["description"] if note["description"] else ""))
    for issue in config.unsupported:
        console.print(Text(issue, style="yellow"))


def _run_inspect(url, *, display=True):
    from .access import require_public_form
    from .inspector import inspect_form

    if display:
        with console.status("Reading form..."):
            schema = inspect_form(url)
    else:
        schema = inspect_form(url)
    require_public_form(schema)
    config = config_from_schema(schema)
    if display:
        _show_form(config)
    return config


class FileSelectionCancelled(Exception):
    """Return to response-source selection after closing the file picker."""


def _select_csv(title="Choose response CSV"):
    while True:
        _ask_text("Press Enter to open the file picker")
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
                selected = filedialog.askopenfilename(
                    parent=root, title=title, filetypes=[("CSV files", "*.csv")]
                )
            finally:
                root.destroy()
        except (ImportError, RuntimeError) as exc:
            raise ValueError(
                "The file picker needs a graphical desktop and Python Tk support. On Linux install python3-tk. Scripted commands accept --data or --like."
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Cannot open the file picker: {exc}. Use a desktop session or a scripted --data/--like command."
            ) from exc
        if not selected:
            raise FileSelectionCancelled()
        path = Path(selected)
        if path.is_file() and path.suffix.lower() == ".csv":
            return path.resolve()
        console.print("Choose an existing .csv file.")


def _mapping(config, headers):
    from .generator import map_columns

    proposed = None
    try:
        proposed = map_columns(config, headers)
    except ValueError as exc:
        console.print(Text(str(exc), style="yellow"))
    if proposed is not None:
        table = Table(title="Match CSV columns to questions", header_style="bold cyan")
        table.add_column("Question")
        table.add_column("CSV column")
        for q in config.questions:
            table.add_row(
                Text(q.text), Text(proposed[q.index] or "(no matching column; answer will be blank)")
            )
        console.print(table)
        unused = [header for header in headers if header not in proposed.values()]
        if unused:
            _info("Unused CSV columns: " + ", ".join(unused))
        if all(value is not None for value in proposed.values()):
            return proposed
        choice = _ask_select(
            "Some optional questions have no matching CSV column",
            [
                _choice("map", "Choose which CSV column answers each question"),
                _choice("blank", "Leave these optional answers blank"),
                _choice("back", "Choose another way to get answers"),
            ],
            default="map",
        )
        if choice == "blank":
            return proposed
        if choice == "back":
            raise FileSelectionCancelled()
    mapping = {}
    used = set()
    for q in config.questions:
        candidates = [
            h
            for h in headers
            if h.casefold().strip() in {q.text.casefold().strip(), f"q{q.index}", f"entry.{q.entry_id}"}
        ]
        if len(candidates) == 1 and candidates[0] not in used:
            column = candidates[0]
        else:
            choices = [_choice(h, h) for h in headers if h not in used]
            if not q.required:
                choices.insert(0, _choice("__blank__", "Leave blank"))
            if not choices:
                raise ValueError(f"Q{q.index}: no source column is available.")
            column = _ask_select(f"Which CSV column answers Q{q.index}: {q.text}?", choices)
            if column == "__blank__":
                column = None
        mapping[q.index] = column
        if column is not None:
            used.add(column)
    return map_columns(config, headers, mapping)


def _import_profiles(config, path):
    from .generator import CheckboxRepairError, load_reference, read_pool
    from .validation import AmbiguousDateError

    headers, _ = read_pool(path)
    mapping = _mapping(config, headers)
    config.pool_column_map = mapping
    repair = False
    while True:
        changes = []
        try:
            profiles = load_reference(config, path, mapping=mapping, repair=repair, changes=changes)
        except AmbiguousDateError as exc:
            console.print(Text(str(exc), style="yellow"))
            config.csv_date_order = _ask_select(
                "Date order in this CSV",
                [
                    _choice("mdy", "Month/day/year (e.g. 10/15/2023)"),
                    _choice("dmy", "Day/month/year (e.g. 15/10/2023)"),
                ],
            )
            continue
        except CheckboxRepairError as exc:
            if repair:
                raise
            console.print(Text(str(exc), style="yellow"))
            if not _ask_confirm(
                "Fix repeated checkbox choices, choices that must be picked alone, and too many selections? Each change will be listed."
            ):
                raise click.Abort() from None
            repair = True
            continue
        for change in changes:
            console.print(Text(change, style="yellow"))
        config._repairs = changes
        return profiles


def _reference_prompt(config, required=False):
    while required or _ask_confirm("Use an existing CSV as a guide?"):
        try:
            path = _select_csv("Choose reference CSV")
            return _import_profiles(config, path)
        except PromptBack:
            if required:
                raise
    return None


def _generate(config, reference=None, instructions="", **options):
    from .ai import complete_responses, generate_responses

    partials = options.pop("partials", None)
    options.pop("compact", None)
    progress = options.pop("progress", lambda done, total: _info(f"Generating responses: {done}/{total}"))
    options.setdefault("browser", config.browser)
    options.setdefault("attention", _ask_text if interactive_input() else None)
    _info(
        "Generating with ChatGPT in a private browser. Form context, instructions, "
        "and reference examples will be sent as text."
    )
    generate = generate_responses if partials is None else complete_responses
    request = config.count if partials is None else partials
    return generate(
        config,
        request,
        reference=reference,
        instructions=instructions,
        progress=progress,
        **options,
    )


def _configure_weights(config):
    for q in config.questions:
        if not q.options:
            continue
        console.print(Text(f"Q{q.index}: {q.text}", style="bold"))
        weights = []
        for index, option in enumerate(q.options):
            default = q.weights[index] if q.weights else 1
            while True:
                try:
                    value = float(
                        _ask_text(f"Chance for {option} (higher = picked more often)", str(default))
                    )
                    import math

                    if math.isfinite(value) and value >= 0:
                        weights.append(value)
                        break
                except ValueError:
                    pass
                console.print("Enter a finite nonnegative number.")
        if not any(weights):
            raise ValueError(f"Q{q.index}: at least one option needs a positive weight.")
        q.weights = weights


def _available_path(value):
    path = clean_path(value)
    number = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        number += 1
    return candidate


def _save_plan(config, answers, path=None, source="prepared", *, cleanup_on_success=True, artifacts=None):
    from .generator import validate_answer
    from .inspector import schema_fingerprint

    answers = [validate_answer(config, answer) for answer in answers]
    if len(answers) != config.count:
        raise ValueError("Prepared response count does not match the requested count.")
    target = clean_path(path) if path else _available_path("gformbot-responses.json")
    if target.exists():
        raise ValueError(f"Output already exists: {target}. Choose a different filename.")
    if config.pool_file and target.resolve() == clean_path(config.pool_file).resolve():
        raise ValueError("A plan cannot overwrite the source CSV.")
    from .artifacts import describe

    payload = {
        "schema_version": 2 if config.media or any(q.allow_other for q in config.questions) else 1,
        "kind": "gformbot-run",
        "created_at": datetime.now().astimezone().isoformat(),
        "form_fingerprint": schema_fingerprint(config),
        "config": config_to_dict(config),
        "batch_id": uuid.uuid4().hex,
        "source": source,
        "repairs": getattr(config, "_repairs", []),
        "responses": answers,
        "cleanup_on_success": cleanup_on_success,
        "temporary_files": describe(
            artifacts if artifacts is not None else getattr(config, "_generated_artifacts", []), target.parent
        ),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    from .locking import file_lock
    from .state import atomic_json

    while True:
        with file_lock(target, timeout=5):
            if target.exists():
                if path is not None:
                    raise ValueError(f"Output already exists: {target}. Choose a different filename.")
                target = _available_path("gformbot-responses.json")
                continue
            atomic_json(target, payload)
            break
    from .previews import remember

    try:
        remember(target)
    except OSError as exc:
        _info(f"Answers saved, but could not remember their location: {exc}. Use the saved file with --data.")
    _info(f"Answers saved: {target.resolve()}")
    return target


def _finish_plan(path, expected_digest, keep_files=False):
    if keep_files:
        _info(f"Saved response files kept: {clean_path(path).resolve()}")
        return
    from .artifacts import cleanup

    removed, preserved = cleanup(path, expected_digest)
    if removed:
        _info("Removed completed response files: " + ", ".join(p.name for p in removed))
    if preserved:
        _info("Kept changed or inaccessible response files: " + ", ".join(str(p) for p in preserved))


def _next_action(count):
    return _ask_select(
        "What next?",
        [
            _choice("submit", f"Submit {count} responses"),
            _choice("edit", "Review or edit responses"),
            _choice("export", "Export remaining responses as CSV"),
            _choice("back", "Back to start"),
        ],
        default="submit",
    )


def _load_plan(path):
    from .generator import validate_answer
    from .inspector import schema_fingerprint

    try:
        payload = json.loads(clean_path(path).read_text(encoding="utf-8-sig"))
        if payload.get("schema_version") not in (1, 2) or payload.get("kind") != "gformbot-run":
            raise ValueError("Unknown response plan format.")
        config = config_from_dict(payload["config"])
        config._instructions_known = "instructions" in payload["config"]
        if schema_fingerprint(config) != payload["form_fingerprint"]:
            raise ValueError("The plan's form schema was changed; prepare a fresh plan.")
        answers = [validate_answer(config, answer) for answer in payload["responses"]]
        if len(answers) != config.count:
            raise ValueError("The saved response count is inconsistent.")
        submitted = payload.get("submitted_indices", [])
        if (
            not isinstance(submitted, list)
            or any(type(i) is not int or not 1 <= i <= config.count for i in submitted)
            or len(set(submitted)) != len(submitted)
        ):
            raise ValueError("The saved submission progress is invalid.")
        from .state import Journal, batch_id

        journal_states = Journal(batch_id(payload)).states()
        config._submitted_indices = sorted(
            set(submitted) | {i for i, state in journal_states.items() if state == "confirmed"}
        )
        config._outcomes = journal_states
        config._submission_started = payload.get("submission_started") is True
        if not Path(config.log_file).is_absolute():
            config.log_file = str(clean_path(path).resolve().parent / config.log_file)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot load response plan {path}: {exc}") from exc
    return config, answers


def _track_plan(config, answers, path, expected_digest, *, interactive=None):
    from .state import Session, UncertainSubmission

    path = clean_path(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    session = Session(path, payload, expected_digest)
    for index in session.uncertain():
        if interactive is False or not interactive_input():
            raise UncertainSubmission(
                f"Response {index} has an uncertain previous outcome. Reopen this batch interactively to resolve it before retrying."
            )
        _info(f"Response {index} may already have been submitted. Check Google Forms before choosing.")
        choice = _ask_select(
            "Was this response already submitted?",
            [
                _choice("cancel", "Go back without retrying"),
                _choice("yes", "I checked: it was submitted (skip it)"),
                _choice("no", "I checked: it was not submitted (try again)"),
            ],
            default="cancel",
        )
        if choice == "cancel":
            raise PromptBack()
        session.reconcile(index, choice == "yes")
    if not session.ids and interactive is False:
        raise ValueError(
            "Every response in this batch was already confirmed submitted. Return to Start or create a new batch."
        )
    config.count = len(session.ids)
    config._submitted_indices = sorted(session.confirmed)
    config._session = session
    config._plan_digest = session.digest

    def record(index):
        session.confirm(index)
        config._plan_digest = session.digest

    config._record_submission = record
    if session.confirmed:
        _info(f"Skipping {len(session.confirmed)} confirmed response(s); {config.count} remain.")
    return [answers[i - 1] for i in session.ids]


def _show_answer(answer, config, index=1, *, sample=False):
    from .answers import display_value
    from .routing import Routing

    active = Routing(config).active(answer)
    table = Table(
        title=f"Sample response — {index}" if sample else f"Prepared response {index}",
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("Q", style="dim")
    table.add_column("Question", ratio=2)
    table.add_column("Answer", ratio=3)
    questions = config.questions[:12] if sample else config.questions
    for q in questions:
        value = answer[q.index]
        text = display_value(value)
        if sample and len(text) > 240:
            text = text[:237] + "…"
        display = (
            (text or "(blank)") if q.index in active else ((text + " (not used)") if text else "(skipped)")
        )
        table.add_row(str(q.index), Text(q.text), Text(display))
    console.print(table)
    if sample and len(config.questions) > len(questions):
        _info("Sample shortened. Open Review or edit responses to see every answer.")


def _pending_preview(config, answers):
    done = set(getattr(config, "_submitted_indices", []))
    remaining = [(i, row) for i, row in enumerate(answers, 1) if i not in done]
    _info(f"{len(remaining)} responses ready" + (f" · {len(done)} already submitted" if done else ""))
    if remaining:
        index, row = remaining[0]
        _show_answer(row, config, index, sample=True)


def _edit_batch(config, answers, path):
    from .artifacts import digest
    from .editor import review
    from .generator import validate_answer
    from .locking import file_lock
    from .state import Journal, atomic_json, batch_id

    path = clean_path(path)
    config, answers = _load_plan(path)
    expected = digest(path)

    def statuses():
        current, _ = _load_plan(path)
        confirmed = set(current._submitted_indices)
        return [
            "confirmed"
            if i in confirmed
            else "uncertain"
            if current._outcomes.get(i) in {"attempted", "reviewing", "uncertain"}
            else "ready"
            for i in range(1, len(answers) + 1)
        ]

    def save(index, candidate):
        nonlocal expected
        with file_lock(path, timeout=5):
            if digest(path) != expected:
                raise ValueError("The response file changed. Close the editor and reopen the batch.")
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            answer = validate_answer(config, candidate)
            Journal(batch_id(payload)).edit(payload, index, answer, lambda data: atomic_json(path, data))
            expected = digest(path)
            return answer

    _info("Opening the local response editor. Choose Done to return here, or press Ctrl+C in this terminal.")
    review(config, answers, save, statuses)
    current, rows = _load_plan(path)
    return current, rows, path


def _manage_batch(config, answers, path, *, keep_files=False):
    from .artifacts import digest

    _pending_preview(config, answers)
    while True:
        done = set(getattr(config, "_submitted_indices", []))
        action = _next_action(len(answers) - len(done))
        if action == "back":
            _info(f"Answers kept: {clean_path(path).resolve()}")
            _info(
                "Continue a started batch from Continue response batch; otherwise choose Start a new form > Use my own answers > Open saved responses."
            )
            return
        if action == "export":
            from .generator import write_csv

            target = _available_path("gformbot-export.csv")
            done = set(getattr(config, "_submitted_indices", []))
            write_csv(config, [row for i, row in enumerate(answers, 1) if i not in done], target)
            _info(f"Export saved: {target.resolve()}")
            continue
        if action == "edit":
            config, answers, path = _edit_batch(config, answers, path)
            _pending_preview(config, answers)
            continue
        try:
            remaining = _track_plan(config, answers, path, digest(path))
        except PromptBack:
            config, answers = _load_plan(path)
            continue
        if not remaining:
            _info("All responses are confirmed submitted.")
            _finish_plan(path, config._plan_digest, keep_files=keep_files)
            return
        save_config(config, DEFAULT_CONFIG_NAME)
        finished = _run_submit(config, answers=remaining)
        _finish_plan(path, config._plan_digest, keep_files=keep_files or finished is False)
        return


@dataclass(frozen=True)
class RunUI:
    _info: Callable
    _section: Callable
    console: Console


def _run_ui():
    return RunUI(_info, _section, console)


def _runner(config, answers):
    from .execution import Runner

    return Runner(config, answers, _run_ui(), getattr(config, "_session", None))


def _run_submit(config, *, answers=None, compact=False):
    from .access import require_public_form

    require_public_form(config)
    if answers is None:
        from .generator import prepare_responses

        answers = prepare_responses(config)
    return _runner(config, answers).run(compact=compact)


@dataclass
class SavedPreview:
    path: Path
    config: RunConfig
    answers: list


def _saved_batches(config=None, *, started_only=False):
    from .inspector import schema_fingerprint
    from .previews import available

    matches, unreadable = [], 0
    for path in available():
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("kind") != "gformbot-run":
                continue
            saved, answers = _load_plan(path)
            if config is not None and schema_fingerprint(saved) != schema_fingerprint(config):
                continue
            if len(saved._submitted_indices) == saved.count:
                continue
            # A journal row also records a definite failure on the first response.
            if started_only and not (
                saved._submission_started or saved._submitted_indices or saved._outcomes
            ):
                continue
            created = datetime.fromisoformat(payload["created_at"]).timestamp()
            matches.append((created, SavedPreview(path, saved, answers)))
        except (ValueError, OSError, KeyError, TypeError, OverflowError):
            unreadable += 1
    matches.sort(key=lambda item: (item[0], str(item[1].path)), reverse=True)
    return matches, unreadable


def _batch_label(batch):
    saved = batch.config
    confirmed = set(saved._submitted_indices)
    uncertain = sum(
        index not in confirmed and state in {"attempted", "reviewing", "uncertain"}
        for index, state in saved._outcomes.items()
    )
    remaining = saved.count - len(confirmed) - uncertain
    status = f"{len(confirmed)}/{saved.count} submitted"
    if uncertain:
        status += f" · {uncertain} needs checking"
    return f"{saved.form_title or 'Untitled form'} | {status} · {remaining} remaining | {batch.path.name}"


def _select_saved_preview(config=None, *, started_only=False):
    from .inspector import schema_fingerprint

    while True:
        matches, unreadable = _saved_batches(config, started_only=started_only)
        _info("Saved response batches, newest first. Confirmed submissions are skipped.")
        if unreadable:
            _info(f"Skipped {unreadable} unreadable response file(s).")
        choices = [_choice(batch, _batch_label(batch)) for _, batch in matches]
        if not choices:
            _info(
                "No unfinished response batches found."
                if started_only
                else "No matching saved responses found. Save some responses first, or open a CSV."
            )
        choices += [_choice("refresh", "Refresh saved responses"), _choice("back", "Back")]
        selected = _ask_select("Continue response batch" if started_only else "Saved responses", choices)
        if selected == "refresh":
            continue
        if selected == "back":
            return None
        # Recheck progress and schema if another process changed the selected file.
        try:
            saved, answers = _load_plan(selected.path)
            if config is not None and schema_fingerprint(saved) != schema_fingerprint(config):
                raise ValueError("This preview now describes another form.")
            if len(saved._submitted_indices) == saved.count:
                raise ValueError("This batch has already finished.")
            if started_only and not (
                saved._submission_started or saved._submitted_indices or saved._outcomes
            ):
                raise ValueError("This batch has not started submission.")
            return SavedPreview(selected.path, saved, answers)
        except ValueError as exc:
            _info(str(exc))


def _prepare_source(config, source):
    from .access import require_public_form

    require_public_form(config)
    if source in {"fresh", "reference"}:
        steps = [lambda _: _reference_prompt(config, required=True)] if source == "reference" else []
        *reference, instructions = prompt_steps(
            *steps, lambda _: _ask_text("Extra instructions (optional, e.g. what to match in your CSV)")
        )
        answers = _generate(config, reference[0] if reference else None, instructions)
        from .generator import write_csv

        output = _available_path("gformbot-generated.csv")
        write_csv(config, answers, output)
        config._generated_artifacts = [output]
        _info(f"CSV saved: {output.resolve()}")
        return answers
    if source == "own":
        while True:
            kind = _ask_select(
                "Use my own answers",
                [
                    _choice("csv", "Open a CSV file (press Enter to choose)"),
                    _choice("saved", "Open saved responses (newest first; no file picker)"),
                    _choice("back", "Back to answer choices"),
                ],
                default="csv",
            )
            if kind == "back":
                return None
            try:
                if kind == "csv":
                    return _own_csv_answers(config)
                selected = _select_saved_preview(config)
            except PromptBack:
                continue
            if selected is not None:
                return selected
    return _random_answers(config)


def _own_csv_answers(config):
    from .generator import prepare_responses

    def read(_):
        path = _select_csv()
        profiles = _import_profiles(config, path)
        config.pool_file = str(path)
        return profiles

    def sampling(_):
        return _ask_select(
            "How should rows be chosen from your CSV?",
            [
                _choice("ordered", "Use rows in order"),
                _choice("shuffle", "Shuffle rows (use each row once)"),
                _choice("sample", "Pick rows randomly (rows can repeat)"),
            ],
            default="ordered",
        )

    def count(selected):
        profiles, mode = selected
        limit = config.max_runs if mode == "sample" else min(config.max_runs, len(profiles))
        if getattr(config, "_prompt_csv_count", False) or config.count > limit:
            return _ask_number("Number of responses", min(config.count, limit), max_val=limit)
        return config.count

    profiles, mode, config.count = prompt_steps(read, sampling, count)
    return prepare_responses(config, profiles, sampling=mode)


def _random_answers(config):
    from .generator import CHOICE_TYPES, apply_reference_weights, make_answer
    from .routing import Routing

    routes = Routing(config)

    other = [q for q in config.questions if q.qtype not in CHOICE_TYPES and q.fixed_value is None]

    def other_answers():
        _info(
            "Random selection supports multiple choice, checkboxes, dropdowns and numeric scales. This form also has text or date questions."
        )
        if _ask_confirm("Auto-generate answers for the other questions?"):
            return True
        if not routes.enabled and any(q.required for q in other):
            _info(
                "Required written/date answers need AI or supplied fixed values. Optional answers may stay blank."
            )
            return None
        return False

    def distribution(selected):
        mode = _ask_select(
            "How often should each choice be picked?",
            [
                _choice("auto", "Automatic (follow your CSV, or use equal chances)"),
                _choice("manual", "Set the chances myself"),
            ],
            default="auto",
        )
        if selected[0]:
            apply_reference_weights(config, selected[0])
        return mode

    def weights(selected):
        if selected[-1] == "manual":
            _configure_weights(config)

    while True:
        use_ai = other_answers() if other else False
        if use_ai is None:
            return None
        steps = (
            [lambda _: _ask_text("Additional instructions for written answers (optional)")] if use_ai else []
        )
        try:
            reference, *extra, mode, _ = prompt_steps(
                lambda _: _reference_prompt(config), *steps, distribution, weights
            )
        except PromptBack:
            if other:
                continue
            raise
        break
    instructions = extra[0] if extra else ""
    validate_config(config)
    import random

    partials = []
    for _ in range(config.count):
        reference_row = random.choice(reference) if reference and mode == "auto" else None
        row = (
            make_answer(config, routing=routes, partial=True)
            if reference_row is None
            else {
                q.index: q.fixed_value if q.fixed_value is not None else reference_row[q.index]
                for q in config.questions
                if q.qtype in CHOICE_TYPES or q.fixed_value is not None
            }
        )
        partials.append(row)
    if use_ai:
        return _generate(config, reference, instructions, partials=partials)
    from .generator import validate_answer

    return [validate_answer(config, {**{q.index: "" for q in other}, **row}) for row in partials]


def _wizard():
    _print_banner()
    _info("Arrow keys to choose, Enter to select, Backspace twice to go back.")
    while True:
        choices = [_choice("quick", "Quick submit"), _choice("new", "Start a new form")]
        if _saved_batches(started_only=True)[0]:
            choices.append(_choice("saved", "Continue response batch"))
        try:
            task = _ask_select(
                "Start",
                choices + [_choice("more", "More options"), _choice("exit", "Exit")],
                default="new",
            )
            if task == "exit":
                return
            if task == "quick":
                _quick_submit()
            elif task == "new":
                _new_batch_wizard()
            elif task == "saved":
                _resume_batch()
            elif task == "more":
                _more_options()
        except PromptBack:
            continue


def _resume_batch():
    while selected := _select_saved_preview(started_only=True):
        try:
            _manage_batch(selected.config, selected.answers, selected.path)
        except PromptBack:
            continue
        return


def _quick_reference():
    while _ask_confirm("Use a CSV as a guide? (optional)", completed=True):
        try:
            return _select_csv("Choose reference CSV")
        except (PromptBack, FileSelectionCancelled):
            pass
    return None


def _quick_submit():
    _section("Quick submit")
    _info("GFormBot will generate and submit responses automatically after the last prompt.")
    url, count, message, like = prompt_steps(
        lambda _: _url_prompt(completed=True),
        lambda _: _ask_number("Number of responses", completed=True),
        lambda _: _ask_text("Extra instructions (optional)", completed=True),
        lambda _: _quick_reference(),
    )
    try:
        _submit_command(url, count, message, like, None, None)
    except ValueError as exc:
        console.print(Text(f"Quick submit could not finish: {exc}", style="yellow"))


def _more_options():
    while True:
        task = _ask_select(
            "More options",
            [
                _choice("inspect", "View a form's questions"),
                _choice("doctor", "Check setup"),
                _choice("back", "Back"),
            ],
        )
        if task == "back":
            return
        if task == "inspect":
            try:
                _run_inspect(_url_prompt())
            except PromptBack:
                continue
            except ValueError as exc:
                console.print(Text(str(exc), style="yellow"))
        elif task == "doctor":
            doctor.callback()


def _new_batch_wizard():
    while True:
        url = _url_prompt()
        try:
            with console.status("Reading form..."):
                config = _run_inspect(url, display=False)
        except ValueError as exc:
            console.print(Text(str(exc), style="yellow"))
            continue
        _info(config.form_title or "Form ready")
        config._prompt_csv_count = True
        try:
            _form_choices(config)
        except PromptBack:
            continue
        return


def _ai_source():
    return _ask_select(
        "How should AI create the answers?",
        [
            _choice("fresh", "Create new fictional responses"),
            _choice("reference", "Use an existing CSV as a guide"),
        ],
        default="fresh",
    )


def _form_choices(config):
    while True:
        source = _ask_select(
            "Where should the answers come from?",
            [
                _choice("generate", "Generate answers with AI"),
                _choice("own", "Use my own answers"),
                _choice("random", "Pick answers randomly (best for choice questions)"),
                _choice("inspect", "View this form's questions"),
            ],
            default="generate",
        )
        if source == "inspect":
            _show_form(config)
            continue

        def set_count(_):
            config.count = _ask_number("Number of new responses", max_val=config.max_runs)

        def prepare(selected, source=source):
            picked = selected[0] if source == "generate" else source
            return picked, _prepare_source(config, picked)

        try:
            if source == "own":
                picked, answers = prepare([])
            else:
                steps = [lambda _: _ai_source()] if source == "generate" else []
                *_, (picked, answers) = prompt_steps(*steps, set_count, prepare)
        except PromptBack:
            continue
        except FileSelectionCancelled:
            _info("No file chosen. Choose another way to get answers.")
            continue
        except ValueError as exc:
            _info(f"Could not prepare responses: {exc}")
            _info("Choose another way to get answers and try again.")
            continue
        if answers is None:
            continue
        config.log_file = "gformbot-runs.csv"
        if isinstance(answers, SavedPreview):
            target, batch_config, rows = answers.path, answers.config, answers.answers
            _info(f"Reusing saved preview: {target}. Response count: {batch_config.count}.")
        else:
            batch_config, rows = config, answers
            target = _save_plan(config, answers, source=picked)
        try:
            _manage_batch(batch_config, rows, target)
        except PromptBack:
            _info(f"Answers kept: {clean_path(target).resolve()}")
            continue
        return


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="gformbot")
@click.pass_context
def cli(ctx):
    """Prepare and submit Google Forms. No arguments starts the wizard."""
    if ctx.invoked_subcommand is None:
        token = wizard_navigation.set(True)
        try:
            _wizard()
        finally:
            wizard_navigation.reset(token)


def _response_options(function):
    """Keep the fast and prepare-only commands on the same input contract."""
    options = [
        click.option(
            "--date-order",
            type=click.Choice(["mdy", "dmy"]),
            help="CSV dates: month/day/year or day/month/year.",
        ),
        click.option(
            "-n",
            "--count",
            type=click.IntRange(1, 500),
            help="Number of responses; uses saved settings or 1.",
        ),
        click.option("-m", "--message", default="", help="Extra instructions for AI answers."),
        click.option(
            "--like",
            type=click.Path(exists=True, dir_okay=False),
            help="Use a CSV as a guide for AI answers.",
        ),
        click.option(
            "--data",
            type=click.Path(exists=True, dir_okay=False),
            help="Use complete CSV rows or saved response JSON instead of AI.",
        ),
        click.option(
            "--browser",
            type=click.Choice(["auto", "chrome", "firefox", "edge"]),
            help="Browser for ChatGPT generation only.",
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return click.argument("url")(function)


def _command_answers(
    url,
    count,
    message,
    like,
    data,
    browser,
    date_order=None,
    *,
    cleanup_on_success=True,
):
    """Reuse the wizard's preparation path without its tables and questions."""
    from .generator import load_reference, prepare_responses
    from .inspector import schema_fingerprint

    if data and (like or message.strip()):
        raise ValueError("--data uses your exact answers; choose it without --like or -m.")
    if like and clean_path(like).suffix.lower() != ".csv":
        raise ValueError("--like needs a CSV file.")
    if data and clean_path(data).suffix.lower() not in {".csv", ".json"}:
        raise ValueError("--data needs a CSV file or saved response JSON.")
    current = _run_inspect(url, display=False)
    config = current
    if Path(DEFAULT_CONFIG_NAME).is_file():
        previous = load_config(DEFAULT_CONFIG_NAME)
        if schema_fingerprint(previous) == schema_fingerprint(current):
            config = previous
            config.instructions = current.instructions
    config.requires_signin = current.requires_signin
    config.one_response = current.one_response
    config.email_collection = current.email_collection
    if date_order is not None:
        config.csv_date_order = date_order
    if data and clean_path(data).suffix.lower() == ".json":
        config, answers = _load_plan(data)
        if schema_fingerprint(config) != schema_fingerprint(current):
            raise ValueError("The saved responses belong to a different or changed form.")
        if config._instructions_known and config.instructions != current.instructions:
            raise ValueError("The form instructions have changed. Prepare new responses for this form.")
        remaining = len(answers) - len(config._submitted_indices)
        if not remaining:
            raise ValueError("All responses in this file were already submitted.")
        if count is not None and count != remaining:
            raise ValueError(
                f"This file has {remaining} remaining responses; -n cannot change its exact answers."
            )
        config.requires_signin = current.requires_signin
        config.one_response = current.one_response
        config.email_collection = current.email_collection
        config.browser = browser or config.browser
        save_config(config, DEFAULT_CONFIG_NAME)
        return config, answers, clean_path(data), False
    if count is not None:
        config.count = count
    source = data or like
    if source and (
        not config.pool_file or clean_path(config.pool_file).resolve() != clean_path(source).resolve()
    ):
        config.pool_column_map = {}
        for question in config.questions:
            question.pool_column = None
    config.browser = browser or config.browser
    config.pool_file = str(clean_path(data).resolve()) if data else None
    validate_config(config)
    if data:
        answers = prepare_responses(config, load_reference(config, data), ordered=True)
    else:
        reference = load_reference(config, like) if like else None
        answers = _generate(config, reference, message, progress=None, compact=True)
    path = _save_plan(
        config,
        answers,
        source="generate" if cleanup_on_success else "prepare",
        cleanup_on_success=cleanup_on_success,
    )
    save_config(config, DEFAULT_CONFIG_NAME)
    return config, answers, path, True


@cli.command()
@_response_options
def generate(
    url,
    count,
    message,
    like,
    data,
    browser,
    date_order=None,
):
    """Generate answers and submit them automatically over HTTP."""
    _submit_command(url, count, message, like, data, browser, date_order)


def _submit_command(url, count, message, like, data, browser, date_order=None):
    from .artifacts import digest

    config, answers, path, owned = _command_answers(
        url,
        count,
        message,
        like,
        data,
        browser,
        date_order,
    )
    remaining = _track_plan(config, answers, path, digest(path), interactive=False)
    finished = _run_submit(config, answers=remaining, compact=True)
    _finish_plan(path, config._plan_digest, keep_files=not owned or finished is False)


@cli.command()
@_response_options
def prepare(url, count, message, like, data, browser, date_order=None):
    """Create and save response JSON without filling or submitting the form."""
    _, _, path, _ = _command_answers(
        url,
        count,
        message,
        like,
        data,
        browser,
        date_order,
        cleanup_on_success=False,
    )
    _info(f'Ready. Open with: gformbot preview "{path.resolve()}"')


@cli.command()
@click.argument("file", required=False, type=click.Path(exists=True, dir_okay=False))
def preview(file):
    """List unfinished saved responses and open a file in its default application."""
    from .previews import available

    if file:
        path = clean_path(file).resolve()
        if path.suffix.lower() != ".json":
            raise ValueError("Choose a saved JSON file.")
    else:
        records = []
        for candidate in available():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if payload.get("kind") == "gformbot-run":
                    config, answers = _load_plan(candidate)
                    count = len(answers) - len(config._submitted_indices)
                    label = f"{config.form_title} | {count} not submitted"
                else:
                    continue
                if count:
                    records.append(
                        (candidate.stat().st_mtime, _choice(candidate, f"{label} | {candidate.name}"))
                    )
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        if not records:
            _info("No unfinished saved responses found.")
            return
        path = _ask_select(
            "Open saved responses (newest first)",
            [choice for _, choice in sorted(records, key=lambda item: item[0], reverse=True)]
            + [_choice("back", "Back")],
        )
        if path == "back":
            return
    if click.launch(str(path)) != 0:
        raise ValueError(f"Could not open {path}. Set a default application for JSON files.")


@cli.command()
@click.option("--check", is_flag=True, help="Return an error if a required local prerequisite is missing.")
def doctor(check=False):
    """Check local prerequisites without launching a browser or making requests."""
    import importlib.util

    from .browser import available_browsers, detect_default_browser

    table = Table(title="Setup", header_style="bold cyan")
    table.add_column("Check")
    table.add_column("Result")
    table.add_row("Python", sys.version.split()[0])
    installed = available_browsers()
    table.add_row("Execution", "Direct HTTP; no browser needed for supplied answers")
    table.add_row(
        "Browsers found", ", ".join(installed) or "None detected (needed for ChatGPT generation only)"
    )
    table.add_row("Default browser", detect_default_browser() if installed else "Unavailable")
    from .selenium_runtime import PACKAGE

    missing = []
    for module in ("urllib3", "selenium", "tkinter", "pyperclip"):
        present = importlib.util.find_spec(PACKAGE if module == "selenium" else module) is not None
        if module == "urllib3" and not present:
            missing.append("HTTP support (urllib3)")
        table.add_row(
            module,
            ("Available (included)" if module == "selenium" and PACKAGE != "selenium" else "Available")
            if present
            else "Missing" + (" (optional)" if module != "urllib3" else ""),
        )
    table.add_row("Output folder", "Writable" if os.access(Path.cwd(), os.W_OK) else "Not writable")
    table.add_row("Driver", "Needed for ChatGPT generation only; resolved on first browser use")
    console.print(table)
    if check and (missing or not os.access(Path.cwd(), os.W_OK)):
        raise click.ClickException(
            "Setup incomplete: " + ", ".join(missing or ["a writable output folder"]) + "."
        )


@cli.command("config")
@click.argument("config_path", default=DEFAULT_CONFIG_NAME)
@click.option("--show", is_flag=True)
@click.option("--edit", is_flag=True)
def config_cmd(config_path, show, edit):
    """View or edit a saved form configuration."""
    path = clean_path(config_path)
    if not path.is_file():
        raise ValueError(
            f"Config not found: {path}. Run gformbot prepare URL or create responses in the wizard first."
        )
    if edit:
        editor = os.environ.get("EDITOR")
        if editor:
            subprocess.run([*shlex.split(editor), str(path)], check=True)
        elif os.name == "nt":
            os.startfile(str(path.resolve()))
        else:
            subprocess.run([shutil.which("nano") or shutil.which("vi") or "vi", str(path)], check=True)
    else:
        from rich.syntax import Syntax

        console.print(Syntax(path.read_text(encoding="utf-8"), "yaml", word_wrap=True))


@cli.command()
@click.argument("log_path", default="gformbot-runs.csv")
@click.option("-n", "--last", default=20, type=click.IntRange(1, 500))
@click.option("--followups", is_flag=True, help="Show saved form instructions and links.")
def log(log_path, last, followups):
    """Show recent run results, including complete response data."""
    if followups:
        from .followups import display
        from .state import Journal

        display(Journal("").followups(log_path=log_path, last=last), _run_ui(), full=True)
        return
    path = clean_path(log_path)
    if not path.exists():
        _info("No run log yet.")
        return
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        data = deque(reader, maxlen=last)
    if not header:
        _info("The run log is empty.")
        return
    table = Table(header_style="bold cyan")
    for column in header:
        table.add_column(Text(column))
    for row in data:
        table.add_row(*(Text(value) for value in row))
    console.print(table)


def main():
    try:
        cli(standalone_mode=False)
    except (KeyboardInterrupt, click.Abort):
        click.echo("Cancelled.", err=True)
        raise SystemExit(130) from None
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except Exception as exc:
        if os.environ.get("GFORMBOT_DEBUG") == "1":
            raise
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from None
