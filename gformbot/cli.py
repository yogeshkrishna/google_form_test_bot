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

console = Console(highlight=False, safe_box=True)
_INQUIRER = sys.stdin.isatty() and sys.stdout.isatty()
BROWSERS = ["headless", "chrome", "chrome-background", "firefox", "edge", "auto", "auto-background"]


@dataclass
class MenuChoice:
    value: object
    name: str


def _choice(value, name):
    return MenuChoice(value, name)


def _ask_text(message, default=""):
    if _INQUIRER:
        from InquirerPy import inquirer

        value = inquirer.text(message=message, default=default).execute()
        if value is None:
            raise click.Abort()
        return value.strip()
    value = input(f"{message}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def _ask_confirm(message, default=False):
    if _INQUIRER:
        from InquirerPy import inquirer

        value = inquirer.confirm(message=message, default=default).execute()
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

        value = inquirer.select(
            message=message, choices=[{"value": c.value, "name": c.name} for c in choices], default=default
        ).execute()
        if value is None:
            raise click.Abort()
        return value
    console.print(Text(message))
    for index, choice in enumerate(choices, 1):
        console.print(Text(f"  {index}. {choice.name}" + (" (default)" if choice.value == default else "")))
    while True:
        value = input("Enter number: ").strip()
        if not value and default is not None:
            return default
        if value.isdecimal() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1].value
        console.print(f"Enter a number between 1 and {len(choices)}.")


def _ask_number(message, default=1, min_val=1, max_val=500):
    while True:
        value = _ask_text(message, str(default))
        if value.isdecimal() and min_val <= int(value) <= max_val:
            return int(value)
        console.print(f"Enter a whole number between {min_val} and {max_val}.")


def _print_banner():
    console.print(
        Panel.fit(
            f"[bold cyan]gformbot[/bold cyan] [dim]{__version__}[/dim]\nPrepare, preview and fill Google Forms.",
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


def _url_prompt():
    url = _clipboard_url()
    if url and _ask_confirm(f"Use the form URL in your clipboard? {url}", default=True):
        return url
    while True:
        url = _ask_text("Paste public Google Form URL")
        valid, message = _validate_form_url(url)
        if valid:
            return url
        console.print(Text(message, style="yellow"))


def _show_form(config):
    from .validation import date_format, needs_email

    _section(config.form_title)
    if config.form_description:
        console.print(Text(config.form_description))
    table = Table(show_lines=True, header_style="bold cyan", expand=True)
    table.add_column("Q", style="dim", no_wrap=True)
    table.add_column("Question / description", ratio=3)
    table.add_column("Type / required", ratio=1)
    table.add_column("Options / constraints", ratio=3)
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
            table.add_row("", Text(section, style="bold cyan"), "", "")
            previous_page = q.page
        for note in notes:
            if note["page"] == q.page:
                show_note(note)
        details = "\n".join(q.options)
        rules = []
        if q.min_select:
            rules.append(f"At least {q.min_select} selections")
        if q.max_select is not None:
            rules.append(f"At most {q.max_select} selections")
        if q.exclusive_options:
            rules.append("Choose alone: " + "; ".join(q.exclusive_options))
        if needs_email(q):
            rules.append("Valid email address")
        elif q.validation and q.qtype != "checkbox":
            rules.append("Custom validation (not supported)")
        if q.qtype == "date":
            rules.append(date_format(q))
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
    for note in getattr(config, "instructions", []):
        if note["before_question"] > len(config.questions):
            _info(note["title"] + ("\n" + note["description"] if note["description"] else ""))
    for issue in config.unsupported:
        console.print(Text(issue, style="yellow"))


def _run_inspect(url, browser="headless", save=False, output=DEFAULT_CONFIG_NAME):
    from .inspector import inspect_form

    with console.status("Reading form..."):
        schema = inspect_form(url, browser=browser, headless=True)
    config = config_from_schema(schema)
    _show_form(config)
    if save:
        save_config(config, output)
        _info(f"Config saved: {clean_path(output).resolve()}")
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
                "The file picker needs a graphical desktop and Python Tk support. On Linux install python3-tk. Scripted commands accept --pool or --reference."
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Cannot open the file picker: {exc}. Use a desktop session or a scripted --pool/--reference command."
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
        table = Table(title="CSV column mapping", header_style="bold cyan")
        table.add_column("Question")
        table.add_column("CSV column")
        for q in config.questions:
            table.add_row(Text(q.text), Text(proposed[q.index] or "(unmatched: will be blank)"))
        console.print(table)
        unused = [header for header in headers if header not in proposed.values()]
        if unused:
            _info("Unused CSV columns: " + ", ".join(unused))
        if all(value is not None for value in proposed.values()):
            return proposed
        choice = _ask_select(
            "Some optional questions have no matched CSV column",
            [
                _choice("map", "Choose their source columns"),
                _choice("blank", "Intentionally leave unmatched optional questions blank"),
                _choice("back", "Choose another response source"),
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
            column = _ask_select(f"Source column for Q{q.index}: {q.text}", choices)
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
                "Explicitly repair checkbox duplicates, exclusive choices and excess selections? Changes will be recorded."
            ):
                raise click.Abort() from None
            repair = True
            continue
        for change in changes:
            console.print(Text(change, style="yellow"))
        config._repairs = changes
        return profiles


def _reference_prompt(config, required=False):
    if required or _ask_confirm("Use an existing CSV as a reference?"):
        path = _select_csv("Choose reference CSV")
        return _import_profiles(config, path)
    return None


def _generate(config, reference=None, instructions="", **options):
    from .ai import complete_responses, generate_responses
    from .previews import remember

    partials = options.pop("partials", None)
    if "browser" not in options:
        options["browser"] = _generation_browser()
    if "checkpoint" not in options:
        options["checkpoint"] = _available_path("gformbot-generation.json")
        config._temporary_checkpoint = Path(options["checkpoint"])
    _info(
        "Opening ChatGPT in a private browser. The form, instructions, and reference examples will be sent to ChatGPT."
    )
    try:
        generate = generate_responses if partials is None else complete_responses
        request = config.count if partials is None else partials
        return generate(
            config,
            request,
            reference=reference,
            instructions=instructions,
            progress=lambda done, total: _info(f"Generating responses: {done}/{total}"),
            attention=_chatgpt_attention if sys.stdin.isatty() else None,
            **options,
        )
    finally:
        checkpoint = Path(options["checkpoint"])
        if checkpoint.is_file():
            try:
                remember(checkpoint)
            except OSError as exc:
                _info(f"Could not remember checkpoint location: {exc}")
            _info(
                f'Generation checkpoint: {checkpoint.resolve()}. Resume with gformbot generation "{checkpoint.resolve()}" --resume'
            )


def _generation_browser():
    if not sys.stdin.isatty():
        return "auto"
    return _ask_select(
        "Browser for generation",
        [
            _choice("auto", "Use the default installed browser"),
            _choice("chrome", "Chrome"),
            _choice("firefox", "Firefox"),
            _choice("edge", "Edge"),
        ],
        default="auto",
    )


def _chatgpt_attention(message):
    _ask_text(message)


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
                    value = float(_ask_text(f"Weight for {option}", str(default)))
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
        "schema_version": 1,
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
    from .state import atomic_json

    atomic_json(target, payload)
    from .previews import remember

    try:
        remember(target)
    except OSError as exc:
        _info(f"Preview saved, but could not remember its location: {exc}. Use the run command below.")
    _info(f"Exact response plan: {target.resolve()}")
    checkpoint = getattr(config, "_temporary_checkpoint", None)
    if checkpoint and checkpoint.is_file() and not checkpoint.is_symlink():
        try:
            completed = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (
                completed.get("kind") == "gformbot-generation"
                and completed.get("responses")
                and all(row is not None for row in completed["responses"])
            ):
                checkpoint.unlink()
        except (OSError, ValueError) as exc:
            _info(f"Saved batch is ready; checkpoint kept: {exc}")
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


def _preview_instructions(path):
    path = clean_path(path).resolve()
    _info("Preview saved. This preview step did not fill or submit the form.")
    _info(f'Inspect the filled form later: gformbot run "{path}" --browser chrome')
    _info(f'Submit later: gformbot run "{path}" --submit')
    _info(
        "Or start gformbot and choose Saved batches to open this batch directly, without reading the form again."
    )


def _next_action(saved=False):
    choices = [] if saved else [_choice("preview", "Save preview and exit")]
    return _ask_select(
        "What next?",
        choices
        + [
            _choice("fill", "Fill for review (you decide when to submit)"),
            _choice("submit", "Fill and submit"),
            _choice("test", "Test filling invisibly (keep the batch; no submission)"),
            _choice("inspect", "Inspect a specific response"),
            _choice("edit", "Edit a response and save a new batch"),
            _choice("export", "Export the remaining responses as CSV"),
            _choice("back", "Back; keep this batch saved"),
        ],
        default="fill",
    )


def _choose_browser():
    return _ask_select(
        "Browser",
        [
            _choice("auto-background", "Background (default installed browser; opens only when chosen)"),
            _choice("chrome-background", "Chrome (background; opens only when you choose, recommended)"),
            _choice("chrome", "Chrome (always visible)"),
            _choice("headless", "Headless Chrome (invisible)"),
            _choice("firefox", "Firefox (visible)"),
            _choice("edge", "Edge (visible)"),
            _choice("auto", "Auto-detect default browser"),
        ],
        default="auto-background",
    )


def _apply_browser(config, browser=None, headless=None):
    if browser is not None:
        config.background = browser.endswith("-background")
        config.browser = browser.removesuffix("-background") if config.background else browser
        config.headless = config.browser == "headless"
    if headless is not None:
        config.headless = headless
        if not headless and config.browser == "headless":
            config.browser = "chrome"
    if headless is False:
        config.background = False
    if config.headless:
        config.background = False


def _load_plan(path):
    from .generator import validate_answer
    from .inspector import schema_fingerprint

    try:
        payload = json.loads(clean_path(path).read_text(encoding="utf-8-sig"))
        if payload.get("schema_version") != 1 or payload.get("kind") != "gformbot-run":
            raise ValueError("Unknown response plan format.")
        config = config_from_dict(payload["config"])
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
        if not Path(config.log_file).is_absolute():
            config.log_file = str(clean_path(path).resolve().parent / config.log_file)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot load response plan {path}: {exc}") from exc
    return config, answers


def _track_plan(config, answers, path, expected_digest):
    from .state import Session, UncertainSubmission

    path = clean_path(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    session = Session(path, payload, expected_digest)
    for index in session.uncertain():
        if not sys.stdin.isatty():
            raise UncertainSubmission(
                f"Response {index} has an uncertain previous outcome. Reopen this batch interactively to resolve it before retrying."
            )
        _info(f"Response {index} may already have been submitted. Check Google Forms before choosing.")
        choice = _ask_select(
            "Previous submission outcome",
            [
                _choice("cancel", "Cancel; keep it unresolved"),
                _choice("yes", "I verified it was submitted; skip it"),
                _choice("no", "I verified it was not submitted; allow retry"),
            ],
            default="cancel",
        )
        if choice == "cancel":
            raise click.Abort()
        session.reconcile(index, choice == "yes")
    if not session.ids:
        raise ValueError(
            "Every response in this preview was already confirmed submitted. Choose View or create a new batch."
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


def _show_answer(answer, config, index=1):
    table = Table(title=f"Prepared response {index}", header_style="bold cyan", show_lines=True)
    table.add_column("Q", style="dim")
    table.add_column("Question", ratio=2)
    table.add_column("Answer", ratio=3)
    for q in config.questions:
        value = answer[q.index]
        text = "\n".join(value) if isinstance(value, list) else value
        table.add_row(str(q.index), Text(q.text), Text(text or "(blank)"))
    console.print(table)


def _show_preview(config, answers):
    _section("Preview")
    for index, answer in enumerate(answers[:3], 1):
        _show_answer(answer, config, index)
    if len(answers) > 3:
        _info(f"Showing 3 of {len(answers)} exact responses; all are in the saved plan.")
    fingerprints = [json.dumps(row, sort_keys=True, ensure_ascii=False) for row in answers]
    blanks = sum(not value for row in answers for value in row.values())
    _info(
        f"Quality summary: {len(fingerprints) - len(set(fingerprints))} duplicate profile(s); {blanks} blank answer(s). Format validation does not prove realism."
    )


def _pending_preview(config, answers):
    done = set(getattr(config, "_submitted_indices", []))
    remaining = [(i, row) for i, row in enumerate(answers, 1) if i not in done]
    if done:
        _info(f"{len(done)} already confirmed; showing remaining original response IDs.")
    for index, row in remaining[:3]:
        _show_answer(row, config, index)
    if len(remaining) > 3:
        _info(f"Showing 3 of {len(remaining)} remaining responses. Choose Inspect to view any response.")


def _edit_batch(config, answers, path):
    from .generator import validate_answer

    done = set(getattr(config, "_submitted_indices", []))
    indexes = [i for i in range(1, len(answers) + 1) if i not in done]
    index = _ask_select(
        "Edit which original response?",
        [_choice(i, f"Response {i}") for i in indexes] + [_choice("back", "Back")],
    )
    if index == "back":
        return config, answers, path
    question = _ask_select("Question", [_choice(q.index, q.text) for q in config.questions])
    current = answers[index - 1][question]
    default = json.dumps(current, ensure_ascii=False) if isinstance(current, list) else current
    while True:
        value = _ask_text("New answer (checkboxes: JSON array; type /blank for empty)", default)
        value = "" if value == "/blank" else value
        candidate = {**answers[index - 1], question: value}
        try:
            candidate = validate_answer(config, candidate)
            break
        except ValueError as exc:
            _info(str(exc))
    fresh = config_from_dict(config_to_dict(config))
    next(q for q in fresh.questions if q.index == question).fixed_value = None
    rows = [json.loads(json.dumps(answers[i - 1])) for i in indexes]
    rows = [validate_answer(fresh, row) for row in rows]
    rows[indexes.index(index)] = candidate
    fresh.count = len(rows)
    return fresh, rows, _save_plan(fresh, rows, source="edited")


def _manage_batch(config, answers, path, *, saved=False):
    from .artifacts import digest

    _pending_preview(config, answers)
    while True:
        action = _next_action(saved=saved) if saved else _next_action()
        if action == "back":
            _info(f"Batch kept: {clean_path(path).resolve()}")
            return
        if action == "preview":
            _preview_instructions(path)
            return
        if action == "inspect":
            index = _ask_number("Original response number", max_val=len(answers))
            _show_answer(answers[index - 1], config, index)
            continue
        if action == "export":
            from .generator import write_csv

            target = _available_path("gformbot-export.csv")
            done = set(getattr(config, "_submitted_indices", []))
            write_csv(config, [row for i, row in enumerate(answers, 1) if i not in done], target)
            _info(f"Export saved: {target.resolve()}")
            continue
        if action == "edit":
            # Resolve possible prior submissions before creating a fresh identity.
            if getattr(config, "_submitted_indices", []) or any(
                state in {"attempted", "reviewing", "uncertain"}
                for state in getattr(config, "_outcomes", {}).values()
            ):
                _track_plan(config, answers, path, digest(path))
            config, answers, path = _edit_batch(config, answers, path)
            saved = False
            _pending_preview(config, answers)
            continue
        remaining = _track_plan(config, answers, path, digest(path))
        if action == "test":
            _apply_browser(config, "headless")
        else:
            _apply_browser(config, _choose_browser())
        _confirm_submit(config, action == "submit")
        save_config(config, DEFAULT_CONFIG_NAME)
        finished = _run_fill(config, answers=remaining, submit=action == "submit")
        _finish_plan(path, config._plan_digest, keep_files=action == "test" or finished is False)
        return


def _runner(config, answers):
    from .execution import Runner

    return Runner(config, answers, sys.modules[__name__], getattr(config, "_session", None))


def _review_mode(config, submit):
    return _runner(config, []).review_mode(submit)


def _background_review(config, pending, answers):
    return _runner(config, answers).background(pending)


def _wait_for_manual_submissions(driver, config, pending, answers, *, tabs=False):
    return _runner(config, answers).wait_manual(driver, pending, tabs=tabs)


def _run_fill(config, dry_run=False, *, answers=None, submit=False):
    if answers is None:
        from .generator import prepare_responses

        answers = prepare_responses(config)
    return _runner(config, answers).run(dry_run=dry_run, submit=submit)


def _confirm_submit(config, submit, yes=False):
    if not submit or yes:
        return
    if not sys.stdin.isatty():
        raise ValueError(
            "Submission needs confirmation. Use --submit --yes only when you intend to submit this exact plan."
        )
    if not _ask_confirm(f"Submit these {config.count} responses to {config.form_title}?"):
        raise click.Abort()


def _validate_execution_flags(dry_run, submit, yes):
    if dry_run and submit:
        raise ValueError(
            "Choose --dry-run to save a preview, or --submit to submit; these cannot be combined."
        )
    if yes and not submit:
        raise ValueError(
            "--yes only confirms submission. Add --submit if you intend to submit, or remove --yes for review."
        )


@dataclass
class SavedPreview:
    path: Path
    config: RunConfig
    answers: list


def _select_saved_preview(config=None):
    from .inspector import schema_fingerprint
    from .previews import available

    while True:
        matches = []
        unreadable = 0
        for path in available():
            try:
                saved, answers = _load_plan(path)
                if config is not None and schema_fingerprint(saved) != schema_fingerprint(config):
                    continue
                if len(saved._submitted_indices) == saved.count:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                created = datetime.fromisoformat(payload["created_at"]).timestamp()
                matches.append((created, SavedPreview(path, saved, answers)))
            except (ValueError, OSError, KeyError, TypeError, OverflowError):
                unreadable += 1
        matches.sort(key=lambda item: (item[0], str(item[1].path)), reverse=True)
        _info(
            "Saved batches, newest first. Confirmed responses are skipped; answers keep their original IDs."
        )
        if unreadable:
            _info(f"Skipped {unreadable} unreadable or unrelated JSON file(s).")
        choices = [
            _choice(
                batch,
                f"{datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M')} | {batch.config.count - len(batch.config._submitted_indices)} remaining / {batch.config.count} total | {batch.path}",
            )
            for created, batch in matches
        ]
        if not choices:
            _info("No compatible saved previews found. Save a preview for this form first, or import a CSV.")
        choices += [_choice("refresh", "Refresh saved previews"), _choice("back", "Back to Use own data")]
        selected = _ask_select("Saved preview", choices)
        if selected == "refresh":
            continue
        if selected == "back":
            return None
        # Validate again in case the file changed while the menu was open.
        try:
            saved, answers = _load_plan(selected.path)
            if config is not None and schema_fingerprint(saved) != schema_fingerprint(config):
                raise ValueError("This preview now describes another form.")
            return SavedPreview(selected.path, saved, answers)
        except ValueError as exc:
            _info(str(exc))


def _prepare_source(config, source):
    if source in {"generate", "enlarge", "fresh", "reference"}:
        reference = (
            None
            if source == "fresh"
            else _reference_prompt(config, required=source in {"enlarge", "reference"})
        )
        instructions = _ask_text("Additional instructions (optional; say what to match in reference data)")
        if source == "enlarge":
            instructions = (
                "Generate additional fictional profiles; avoid merely copying entire reference rows. "
                + instructions
            )
        answers = _generate(config, reference, instructions)
        from .generator import write_csv

        output = _available_path(
            "gformbot-additional.csv" if source == "enlarge" else "gformbot-generated.csv"
        )
        write_csv(config, answers, output)
        config._generated_artifacts = [output]
        _info(f"CSV saved: {output.resolve()}")
        return answers
    if source == "own":
        from .generator import prepare_responses

        while True:
            kind = _ask_select(
                "Use own data",
                [
                    _choice("csv", "Import a CSV (opens file picker after Enter)"),
                    _choice("saved", "Saved preview (newest first; no file picker)"),
                    _choice("back", "Back to response sources"),
                ],
                default="csv",
            )
            if kind == "back":
                return None
            if kind == "csv":
                break
            selected = _select_saved_preview(config)
            if selected is not None:
                return selected
        path = _select_csv()
        profiles = _import_profiles(config, path)
        config.pool_file = str(path)
        mode = _ask_select(
            "Use complete CSV rows",
            [
                _choice("ordered", "Original order"),
                _choice("shuffle", "Shuffle without repetition"),
                _choice("sample", "Sample whole rows with repetition"),
            ],
            default="ordered",
        )
        if getattr(config, "_prompt_csv_count", False):
            limit = min(config.max_runs, len(profiles)) if mode in {"ordered", "shuffle"} else config.max_runs
            config.count = _ask_number("Number of responses", min(config.count, limit), max_val=limit)
        if mode in {"ordered", "shuffle"} and config.count > len(profiles):
            _info(f"The CSV has {len(profiles)} rows. Choose a count within that range.")
            config.count = _ask_number(
                "Number of responses",
                min(config.count, len(profiles)),
                max_val=min(config.max_runs, len(profiles)),
            )
        return prepare_responses(config, profiles, sampling=mode)
    from .generator import CHOICE_TYPES, _random_from_options, apply_reference_weights

    other = [q for q in config.questions if q.qtype not in CHOICE_TYPES and q.fixed_value is None]
    use_ai = False
    if other:
        _info(
            "Random selection supports multiple choice, checkboxes, dropdowns and numeric scales. This form also has text or date questions."
        )
        use_ai = _ask_confirm("Auto-generate answers for the other questions?")
        if not use_ai and any(q.required for q in other):
            _info(
                "Required written/date answers need AI or supplied fixed values. Optional answers may stay blank."
            )
            return None
    reference = _reference_prompt(config)
    instructions = _ask_text("Additional instructions for written answers (optional)") if use_ai else ""
    if reference:
        apply_reference_weights(config, reference)
    distribution = _ask_select(
        "Choice-answer distribution",
        [
            _choice("auto", "Automatic (reference frequencies if supplied; otherwise uniform)"),
            _choice("manual", "Set frequencies manually"),
        ],
        default="auto",
    )
    if distribution == "manual":
        _configure_weights(config)
    validate_config(config)
    import random

    partials = []
    for _ in range(config.count):
        reference_row = random.choice(reference) if reference and distribution == "auto" else None
        partials.append(
            {
                q.index: q.fixed_value
                if q.fixed_value is not None
                else reference_row[q.index]
                if reference_row is not None
                else _random_from_options(q)
                for q in config.questions
                if q.fixed_value is not None or q.qtype in CHOICE_TYPES
            }
        )
    if use_ai:
        return _generate(config, reference, instructions, partials=partials)
    from .generator import validate_answer

    return [validate_answer(config, {**{q.index: "" for q in other}, **row}) for row in partials]


def _wizard():
    _print_banner()
    while True:
        task = _ask_select(
            "Start",
            [
                _choice("new", "Create a new response batch"),
                _choice("saved", "Resume a saved batch"),
                _choice("generation", "Resume interrupted generation"),
                _choice("inspect", "Inspect a form"),
                _choice("doctor", "Check setup"),
                _choice("exit", "Exit"),
            ],
            default="new",
        )
        if task == "exit":
            return
        if task == "new":
            return _new_batch_wizard()
        if task == "saved":
            selected = _select_saved_preview()
            if selected:
                return _manage_batch(selected.config, selected.answers, selected.path, saved=True)
        elif task == "generation":
            from .previews import available

            jobs = []
            for path in available():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    if payload.get("kind") == "gformbot-generation":
                        done = sum(row is not None for row in payload["responses"])
                        if done < payload["request"]["count"]:
                            jobs.append(
                                (
                                    path.stat().st_mtime,
                                    _choice(
                                        path, f"{path.name}: {done}/{payload['request']['count']} prepared"
                                    ),
                                )
                            )
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
            if not jobs:
                _info("No saved generation checkpoints found.")
                continue
            selected = _ask_select(
                "Generation checkpoint (newest first)",
                [item for _, item in sorted(jobs, key=lambda item: item[0], reverse=True)]
                + [_choice("back", "Back")],
            )
            if selected != "back":
                return generation.callback(str(selected), resume=False, export=None)
        elif task == "inspect":
            _run_inspect(_url_prompt())
        else:
            doctor.callback()


def _new_batch_wizard():
    _print_banner()
    _info("Arrow keys to choose; Enter to select; Ctrl+C to cancel.")
    config = _run_inspect(_url_prompt())
    if config.unsupported:
        raise ValueError("This form contains unsupported questions: " + "; ".join(config.unsupported))
    config._prompt_csv_count = True
    while True:
        source = _ask_select(
            "Response source",
            [
                _choice("generate", "Generate responses"),
                _choice("own", "Use own data"),
                _choice("random", "Random (recommended for choice questions)"),
            ],
            default="generate",
        )
        if source == "generate":
            source = _ask_select(
                "Generate",
                [
                    _choice("fresh", "Create fresh synthetic profiles"),
                    _choice("reference", "Match the style and distribution of a reference CSV"),
                    _choice("enlarge", "Generate additional profiles from an existing CSV"),
                ],
                default="fresh",
            )
        if source != "own":
            config.count = _ask_number(
                "Number of additional responses" if source == "enlarge" else "Number of responses",
                max_val=config.max_runs,
            )
        try:
            answers = _prepare_source(config, source)
        except FileSelectionCancelled:
            _info("File selection cancelled. Choose a response source.")
            continue
        except ValueError as exc:
            _info(f"Could not prepare responses: {exc}")
            _info("Choose a response source to try again.")
            continue
        if answers is not None:
            break
    config.log_file = "gformbot-runs.csv"
    reusing_preview = isinstance(answers, SavedPreview)
    if reusing_preview:
        target, config, answers = answers.path, answers.config, answers.answers
        _info(f"Reusing saved preview: {target}. Response count: {config.count}.")
    else:
        target = _save_plan(config, answers, source=source)
    _manage_batch(config, answers, target, saved=reusing_preview)


def _resolve_config(url, config_path=None, pool=None, browser=None, headless=None, count=None):
    if url:
        config = _run_inspect(url)
        if config_path:
            previous = load_config(config_path)
            from .inspector import schema_fingerprint

            if schema_fingerprint(previous) != schema_fingerprint(config):
                raise ValueError("The supplied config describes a different or changed form.")
            config = previous
    elif config_path or Path(DEFAULT_CONFIG_NAME).exists():
        config = load_config(config_path or DEFAULT_CONFIG_NAME)
        if any(not q.entry_id or not q.item_id for q in config.questions):
            raise ValueError(
                "This config predates reliable form inspection. Run gformbot inspect URL to refresh it."
            )
    else:
        raise ValueError("Provide a form URL or --config file.")
    if pool is not None:
        config.pool_file = str(clean_path(pool).resolve())
    _apply_browser(config, browser, headless)
    if count is not None:
        config.count = count
    validate_config(config)
    return config


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="gformbot")
@click.pass_context
def cli(ctx):
    """Prepare, preview and fill Google Forms. No arguments starts the wizard."""
    if ctx.invoked_subcommand is None:
        _wizard()


@cli.command()
@click.argument("url")
@click.option("--save/--no-save", default=True)
@click.option("-o", "--output", default=DEFAULT_CONFIG_NAME)
@click.option("--browser", type=click.Choice(BROWSERS), default="headless", hidden=True)
def inspect(url, save, output, browser):
    """Display the whole form and optionally save its configuration."""
    _run_inspect(url, browser=browser, save=save, output=output)


@cli.command()
@click.argument("url", required=False)
@click.option("-c", "--config", "config_path")
@click.option("-n", "--count", type=click.IntRange(1, 500))
@click.option("--pool", type=click.Path(exists=True, dir_okay=False))
@click.option("--browser", type=click.Choice(BROWSERS))
@click.option("--headless/--no-headless", default=None)
@click.option("--ordered", is_flag=True, help="Use CSV rows in original order.")
@click.option(
    "--sampling",
    type=click.Choice(["ordered", "shuffle", "sample"]),
    help="CSV row selection; sample permits repetition.",
)
@click.option("--repair", is_flag=True, help="Explicitly repair and record supported checkbox issues.")
@click.option("--dry-run", is_flag=True, help="Save and preview answers without opening a browser.")
@click.option("--submit", is_flag=True, help="Submit after filling; otherwise stop before Submit.")
@click.option("--yes", is_flag=True, help="Confirm --submit for scripts.")
@click.option("--keep-files", is_flag=True, help="Keep the response plan after a successful run.")
@click.option(
    "--date-order", type=click.Choice(["mdy", "dmy"]), help="Resolve ambiguous numeric dates in CSV input."
)
def fill(
    url,
    config_path,
    count,
    pool,
    browser,
    headless,
    ordered,
    repair,
    dry_run,
    submit,
    yes,
    keep_files,
    date_order,
    sampling=None,
):
    """Prepare exact answers, then fill the form; submission is opt-in."""
    _validate_execution_flags(dry_run, submit, yes)
    if ordered and sampling not in (None, "ordered"):
        raise ValueError("--ordered cannot be combined with a different --sampling mode.")
    from .generator import load_reference, prepare_responses

    config = _resolve_config(url, config_path, pool, browser, headless, count)
    if date_order:
        config.csv_date_order = date_order
    if browser is None and headless is None and config.headless:
        config.browser, config.headless = "chrome", False
    changes = []
    profiles = (
        load_reference(config, config.pool_file, repair=repair, changes=changes) if config.pool_file else None
    )
    config._repairs = changes
    answers = prepare_responses(config, profiles, ordered=ordered, sampling=sampling)
    target = _save_plan(config, answers)
    from .artifacts import digest

    plan_digest = digest(target)
    _show_preview(config, answers)
    if not dry_run:
        answers = _track_plan(config, answers, target, plan_digest)
        _confirm_submit(config, submit, yes)
    finished = _run_fill(config, dry_run=dry_run, answers=answers, submit=submit)
    if not dry_run:
        _finish_plan(target, config._plan_digest, keep_files or finished is False)
    else:
        _preview_instructions(target)


@cli.command()
@click.argument("url", required=False)
@click.option("-c", "--config", "config_path")
@click.option("-n", "--count", default=None, type=click.IntRange(1, 500))
@click.option("--pool", type=click.Path(exists=True, dir_okay=False))
@click.option("--plan", type=click.Path(exists=True, dir_okay=False))
def preview(url, config_path, count, pool, plan):
    """Preview a saved plan, or prepare and save a fresh batch."""
    from .generator import prepare_responses

    if plan:
        if url or config_path or pool or count is not None:
            raise ValueError("--plan cannot be combined with a form URL, --config, --pool or --count.")
        config, answers = _load_plan(plan)
    else:
        config = _resolve_config(url, config_path, pool=pool, count=count or 5)
        answers = prepare_responses(config)
        target = _save_plan(config, answers)
    _show_preview(config, answers)
    _preview_instructions(plan or target)


@cli.command()
@click.argument("url")
@click.option("-n", "--count", default=10, type=click.IntRange(1, 500))
@click.option("--reference", type=click.Path(exists=True, dir_okay=False))
@click.option("--instructions", default="")
@click.option(
    "--browser", type=click.Choice(["chrome", "firefox", "edge", "auto"]), default="auto", show_default=True
)
@click.option(
    "--date-order",
    type=click.Choice(["mdy", "dmy"]),
    help="Resolve ambiguous numeric dates in the reference CSV.",
)
@click.option("-o", "--output", default="gformbot-generated.csv")
@click.option(
    "--checkpoint",
    type=click.Path(dir_okay=False),
    help="Durable generation job; reusing it resumes matching unfinished work.",
)
def generate(url, count, reference, instructions, browser, date_order, output, checkpoint=None):
    """Ask ChatGPT in a private browser and save validated CSV plus JSON."""
    from .generator import load_reference, write_csv

    config = _run_inspect(url)
    config.count = count
    config.csv_date_order = date_order
    target = clean_path(output)
    if target.suffix.lower() != ".csv":
        raise ValueError("The output filename must end in .csv.")
    if target.exists() or target.with_suffix(".json").exists():
        raise ValueError("Output already exists. Choose a different --output filename.")
    if checkpoint and clean_path(checkpoint).resolve() in {
        target.resolve(),
        target.with_suffix(".json").resolve(),
    }:
        raise ValueError("The generation checkpoint must differ from the output CSV and response plan.")
    if reference and clean_path(reference).resolve() in {
        target.resolve(),
        target.with_suffix(".json").resolve(),
    }:
        raise ValueError("Output cannot overwrite the reference CSV.")
    profiles = load_reference(config, reference) if reference else None
    options = {"browser": browser}
    if checkpoint:
        options["checkpoint"] = checkpoint
    elif _available_path("gformbot-generation.json").resolve() in {
        target.resolve(),
        target.with_suffix(".json").resolve(),
    }:
        options["checkpoint"] = _available_path("gformbot-generation-job.json")
    answers = _generate(config, profiles, instructions, **options)
    write_csv(config, answers, target)
    plan = _save_plan(
        config,
        answers,
        target.with_suffix(".json"),
        source="generate",
        cleanup_on_success=False,
        artifacts=[target],
    )
    _show_preview(config, answers)
    _info(f"CSV saved: {target.resolve()}")
    _preview_instructions(plan)


@cli.command()
@click.argument("plan", type=click.Path(exists=True, dir_okay=False))
@click.option("--browser", type=click.Choice(BROWSERS))
@click.option("--headless/--no-headless", default=None)
@click.option("--dry-run", is_flag=True)
@click.option("--submit", is_flag=True)
@click.option("--yes", is_flag=True)
@click.option("--keep-files", is_flag=True, help="Keep generated response files after a successful run.")
def run(plan, browser, headless, dry_run, submit, yes, keep_files):
    """Replay the exact answers in a saved response plan."""
    _validate_execution_flags(dry_run, submit, yes)
    config, answers = _load_plan(plan)
    from .artifacts import digest

    plan_digest = digest(plan)
    _pending_preview(config, answers)
    interactive = sys.stdin.isatty() and not (
        dry_run or submit or yes or browser is not None or headless is not None
    )
    if interactive:
        _manage_batch(config, answers, clean_path(plan), saved=True)
        return
    if browser:
        _apply_browser(config, browser)
    elif headless is None and config.headless:
        config.browser, config.headless = "chrome", False
    _apply_browser(config, headless=headless)
    if not dry_run:
        answers = _track_plan(config, answers, plan, plan_digest)
        _confirm_submit(config, submit, yes)
    finished = _run_fill(config, dry_run=dry_run, answers=answers, submit=submit)
    if not dry_run:
        _finish_plan(plan, config._plan_digest, keep_files or finished is False)
    else:
        _preview_instructions(plan)


@cli.command()
def batches():
    """List saved response batches offline, newest first."""
    from .previews import available

    records = []
    for path in available():
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            config, answers = _load_plan(path)
            records.append((payload.get("created_at", ""), path, config, len(answers)))
        except (OSError, ValueError):
            continue
    table = Table(title="Saved batches", header_style="bold cyan")
    for label in ("Created", "Form", "Remaining / total", "File"):
        table.add_column(label, overflow="fold")
    for created, path, config, total in sorted(records, key=lambda item: item[0], reverse=True):
        table.add_row(
            Text(created),
            Text(config.form_title),
            str(total - len(config._submitted_indices)) + " / " + str(total),
            Text(str(path)),
        )
    console.print(table)


@cli.command()
@click.argument("checkpoint", type=click.Path(exists=True, dir_okay=False))
@click.option("--resume", is_flag=True, help="Generate only the unfinished profiles.")
@click.option(
    "--export", type=click.Path(dir_okay=False), help="Export completed profiles without opening a browser."
)
def generation(checkpoint, resume, export):
    """Resume a saved generation job, or export its valid partial results."""
    from .generator import validate_answer, write_csv

    path = clean_path(checkpoint)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("kind") != "gformbot-generation" or payload.get("schema_version") != 1:
            raise ValueError("Unknown generation checkpoint format.")
        request = payload["request"]
        config = config_from_dict(request["config"])
        rows = payload["responses"]
        if (
            type(request["count"]) is not int
            or not 1 <= request["count"] <= config.max_runs
            or not isinstance(rows, list)
            or len(rows) != request["count"]
        ):
            raise ValueError("Checkpoint response count is inconsistent.")
        valid = [validate_answer(config, row) for row in rows if row is not None]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot open generation checkpoint: {exc}") from exc
    if resume and export:
        raise ValueError("Choose --resume or --export, not both.")
    _info(f"Generation has {len(valid)}/{len(rows)} valid profiles saved.")
    if not resume and not export:
        choice = _ask_select(
            "Generation checkpoint",
            [
                _choice("resume", "Resume missing profiles"),
                _choice("export", "Export valid profiles only"),
                _choice("back", "Back"),
            ],
            default="resume",
        )
        if choice == "back":
            return
        resume = choice == "resume"
        if not resume:
            export = str(_available_path("gformbot-partial.csv"))
    if export:
        if not valid:
            raise ValueError("No completed profiles are available to export yet.")
        if clean_path(export).suffix.lower() != ".csv":
            raise ValueError("The export filename must end in .csv.")
        _info(f"Export saved: {write_csv(config, valid, export).resolve()}")
        return
    from .ai import complete_responses, generate_responses

    options = {
        "reference": request.get("reference"),
        "instructions": request.get("instructions", ""),
        "checkpoint": path,
        "browser": _generation_browser() if len(valid) < len(rows) else "auto",
        "attention": _chatgpt_attention if sys.stdin.isatty() else None,
        "progress": lambda done, total: _info(f"Generating responses: {done}/{total}"),
    }
    _info("Resuming through ChatGPT; the saved form, instructions and reference context will be sent.")
    partials = request.get("partials")
    answers = (
        complete_responses(config, partials, **options)
        if partials is not None
        else generate_responses(config, request["count"], **options)
    )
    config.count = len(answers)
    output = _available_path("gformbot-generated.csv")
    write_csv(config, answers, output)
    plan = _save_plan(
        config, answers, source="resumed-generation", cleanup_on_success=False, artifacts=[output]
    )
    _show_preview(config, answers)
    _preview_instructions(plan)


@cli.command()
def doctor():
    """Check local prerequisites without launching a browser or making requests."""
    import importlib.util

    from .browser import available_browsers, detect_default_browser

    table = Table(title="Setup", header_style="bold cyan")
    table.add_column("Check")
    table.add_column("Result")
    table.add_row("Python", sys.version.split()[0])
    installed = available_browsers()
    table.add_row("Browsers found", ", ".join(installed) or "None detected; install Chrome, Firefox or Edge")
    table.add_row("Default browser", detect_default_browser() if installed else "Unavailable")
    for module in ("selenium", "tkinter", "pyperclip"):
        present = importlib.util.find_spec(module) is not None
        table.add_row(
            module,
            "Available"
            if present
            else "Missing" + (" (optional clipboard support)" if module == "pyperclip" else ""),
        )
    table.add_row("Output folder", "Writable" if os.access(Path.cwd(), os.W_OK) else "Not writable")
    table.add_row("Driver", "Selenium Manager resolves it on first browser use; internet may be needed")
    console.print(table)


@cli.command("config")
@click.argument("config_path", default=DEFAULT_CONFIG_NAME)
@click.option("--show", is_flag=True)
@click.option("--edit", is_flag=True)
def config_cmd(config_path, show, edit):
    """View or edit a saved form configuration."""
    path = clean_path(config_path)
    if not path.is_file():
        raise ValueError(f"Config not found: {path}. Run gformbot inspect URL first.")
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
def log(log_path, last):
    """Show recent run results, including complete response data."""
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
        console.print("Cancelled.")
        raise SystemExit(130) from None
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except (ValueError, OSError) as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        raise SystemExit(1) from None
    except Exception as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        raise SystemExit(1) from None
