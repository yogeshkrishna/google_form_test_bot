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
        url = _ask_text("Paste your public Google Form link")
        valid, message = _validate_form_url(url)
        if valid:
            return url
        console.print(Text(message, style="yellow"))


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
    for note in getattr(config, "instructions", []):
        if note["before_question"] > len(config.questions):
            _info(note["title"] + ("\n" + note["description"] if note["description"] else ""))
    for issue in config.unsupported:
        console.print(Text(issue, style="yellow"))


def _form_browser(config=None):
    ctx = click.get_current_context(silent=True)
    resource = getattr(config, "_form_browser", None)
    return resource or (ctx.meta.get("form_browser") if ctx else None)


def _open_form_browser(browser):
    if not sys.stdin.isatty():
        raise ValueError("Google sign-in needs an interactive terminal. Run again with --sign-in.")
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        raise ValueError("Start sign-in through gformbot or gformbot generate/prepare --sign-in.")
    resource = _form_browser()
    if resource is None:
        from .browser import FormBrowser

        resource = FormBrowser(browser, _ask_text)
        ctx.meta["form_browser"] = resource
        ctx.call_on_close(resource.close)
    return resource


def _check_response_limit(config, count=None):
    if config.one_response is True and (config.count if count is None else count) > 1:
        raise ValueError(
            "This form allows one response per Google account. Choose 1 response, "
            "or ask the owner for a test copy without that limit."
        )


def _run_inspect(
    url, browser="headless", save=False, output=DEFAULT_CONFIG_NAME, *, display=True, sign_in=False
):
    from .access import FormAccessError
    from .inspector import inspect_form

    resource = _open_form_browser(browser) if sign_in else _form_browser()
    if resource:
        schema = resource.inspect(url)
    else:
        try:
            if display:
                with console.status("Reading form..."):
                    schema = inspect_form(url, browser=browser, headless=True)
            else:
                schema = inspect_form(url, browser=browser, headless=True)
            if schema.requires_signin:
                raise FormAccessError("login_required")
        except FormAccessError as exc:
            if exc.state != "login_required" or not display or not sys.stdin.isatty():
                raise
            if not _ask_confirm("This form needs Google sign-in. Open a browser to sign in?"):
                raise click.Abort() from None
            resource = _open_form_browser(browser)
            schema = resource.inspect(url)
    config = config_from_schema(schema)
    if resource:
        config._form_browser = resource
    if display:
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
    if required or _ask_confirm("Use an existing CSV as a guide?"):
        path = _select_csv("Choose reference CSV")
        return _import_profiles(config, path)
    return None


def _generate(config, reference=None, instructions="", **options):
    from .ai import complete_responses, generate_responses
    from .previews import remember

    partials = options.pop("partials", None)
    compact = options.pop("compact", False)
    completed = False
    progress = options.pop("progress", lambda done, total: _info(f"Generating responses: {done}/{total}"))
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
        answers = generate(
            config,
            request,
            reference=reference,
            instructions=instructions,
            progress=progress,
            attention=_chatgpt_attention if sys.stdin.isatty() else None,
            **options,
        )
        completed = True
        return answers
    finally:
        checkpoint = Path(options["checkpoint"])
        if checkpoint.is_file():
            try:
                remember(checkpoint)
            except OSError as exc:
                _info(f"Could not remember where AI progress was saved: {exc}")
            if not compact or not completed:
                _info(
                    f"AI progress saved: {checkpoint.resolve()}. Open gformbot > Open saved AI work to continue."
                )


def _generation_browser():
    if not sys.stdin.isatty():
        return "auto"
    return _ask_select(
        "Which browser should open ChatGPT?",
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
        "schema_version": 2 if any(q.allow_other for q in config.questions) else 1,
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
    _info(f"Answers saved: {target.resolve()}")
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
            _info(f"Answers are saved; the AI progress file was also kept: {exc}")
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
    _info("Answers saved. Nothing has been filled or submitted.")
    config, _ = _load_plan(path)
    _info(f'Review later: gformbot generate "{config.url}" --data "{path}" --halt')
    _info(f'Submit later: gformbot generate "{config.url}" --data "{path}"')
    _info("Or start gformbot and choose Open saved responses to use these answers again.")


def _next_action(saved=False):
    choices = [] if saved else [_choice("preview", "Save answers and exit")]
    return _ask_select(
        "What next?",
        choices
        + [
            _choice("fill", "Fill for review (I click Submit myself)"),
            _choice("submit", "Fill and submit automatically"),
            _choice("test", "Test without a window (no submission; answers stay saved)"),
            _choice("inspect", "View one response"),
            _choice("edit", "Edit a response and save a copy"),
            _choice("export", "Save remaining responses as a CSV"),
            _choice("back", "Go back (keep my answers saved)"),
        ],
        default="fill",
    )


def _choose_browser():
    return _ask_select(
        "Browser",
        [
            _choice("auto-background", "Open only when I choose (default browser, recommended)"),
            _choice("chrome-background", "Open only when I choose (Chrome)"),
            _choice("chrome", "Chrome (always visible)"),
            _choice("headless", "Chrome (no visible window)"),
            _choice("firefox", "Firefox (visible)"),
            _choice("edge", "Edge (visible)"),
            _choice("auto", "Use my default browser (visible)"),
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
        if interactive is False or not sys.stdin.isatty():
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
    from .answers import display_value
    from .routing import Routing

    active = Routing(config).active(answer)
    table = Table(title=f"Prepared response {index}", header_style="bold cyan", show_lines=True)
    table.add_column("Q", style="dim")
    table.add_column("Question", ratio=2)
    table.add_column("Answer", ratio=3)
    for q in config.questions:
        value = answer[q.index]
        text = display_value(value)
        display = (
            (text or "(blank)") if q.index in active else ((text + " (not used)") if text else "(skipped)")
        )
        table.add_row(str(q.index), Text(q.text), Text(display))
    console.print(table)


def _show_preview(config, answers):
    from .routing import Routing

    _section("Preview")
    for index, answer in enumerate(answers[:3], 1):
        _show_answer(answer, config, index)
    if len(answers) > 3:
        _info(f"Showing 3 of {len(answers)} responses; all answers are in your saved file.")
    routes = Routing(config)
    used = [{index: row[index] for index in routes.active(row)} for row in answers]
    fingerprints = [json.dumps(row, sort_keys=True, ensure_ascii=False) for row in used]
    blanks = sum(not value for row in used for value in row.values())
    _info(
        f"Quick check: {len(fingerprints) - len(set(fingerprints))} repeated response(s); {blanks} blank answer(s). Please also check that the answers make sense."
    )


def _pending_preview(config, answers):
    done = set(getattr(config, "_submitted_indices", []))
    remaining = [(i, row) for i, row in enumerate(answers, 1) if i not in done]
    if done:
        _info(f"{len(done)} already submitted; showing the rest with their original response numbers.")
    for index, row in remaining[:3]:
        _show_answer(row, config, index)
    if len(remaining) > 3:
        _info(f"Showing 3 of {len(remaining)} remaining responses. Choose View one response to see another.")


def _edit_batch(config, answers, path):
    from .generator import validate_answer
    from .routing import Routing

    done = set(getattr(config, "_submitted_indices", []))
    indexes = [i for i in range(1, len(answers) + 1) if i not in done]
    index = _ask_select(
        "Which response would you like to edit?",
        [_choice(i, f"Response {i}") for i in indexes] + [_choice("back", "Back")],
    )
    if index == "back":
        return config, answers, path
    question = _ask_select("Question", [_choice(q.index, q.text) for q in config.questions])
    current = answers[index - 1][question]
    routes = Routing(config)
    previously_used = routes.active(answers[index - 1])
    default = json.dumps(current, ensure_ascii=False) if isinstance(current, (list, dict)) else current
    while True:
        value = _ask_text("New answer (checkboxes: JSON array; type /blank for empty)", default)
        value = "" if value == "/blank" else value
        candidate = {**answers[index - 1], question: value}
        try:
            candidate[question] = validate_answer(config, {question: value}, partial=True, routing=routes)[
                question
            ]
            if routes.enabled:
                for page in routes.walk(candidate):
                    for q in routes.questions[page]:
                        if q.index in previously_used or q.index == question:
                            continue
                        while True:
                            try:
                                validate_answer(
                                    config, {q.index: candidate[q.index]}, partial=True, routing=routes
                                )
                                break
                            except ValueError as exc:
                                _info(str(exc))
                                entry = _ask_text(f"Answer needed for this route: {q.text}")
                                candidate[q.index] = "" if entry == "/blank" else entry
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


def _manage_batch(config, answers, path, *, saved=False, keep_files=False):
    from .artifacts import digest

    _pending_preview(config, answers)
    while True:
        action = _next_action(saved=saved) if saved else _next_action()
        if action == "back":
            _info(f"Answers kept: {clean_path(path).resolve()}")
            return
        if action == "preview":
            _preview_instructions(path)
            return
        if action == "inspect":
            index = _ask_number("Response number", max_val=len(answers))
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
        elif _form_browser(config):
            _info("Using the browser you signed in with.")
        else:
            _apply_browser(config, _choose_browser())
        _confirm_submit(config, action == "submit")
        save_config(config, DEFAULT_CONFIG_NAME)
        finished = _run_fill(config, answers=remaining, submit=action == "submit")
        _finish_plan(
            path, config._plan_digest, keep_files=keep_files or action == "test" or finished is False
        )
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


def _run_fill(config, dry_run=False, *, answers=None, submit=False, halt=False, compact=False):
    resource = _form_browser(config)
    if not dry_run and config.requires_signin and resource is None:
        resource = _open_form_browser(config.browser)
    if resource:
        config._form_browser = resource
    if answers is None:
        from .generator import prepare_responses

        answers = prepare_responses(config)
    options = {"dry_run": dry_run, "submit": submit}
    if halt:
        options["halt"] = True
    if compact:
        options["compact"] = True
    return _runner(config, answers).run(**options)


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
            "Saved responses, newest first. Already submitted responses are skipped; response numbers stay the same."
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
            _info("No matching saved responses found. Save some responses first, or open a CSV.")
        choices += [_choice("refresh", "Refresh saved responses"), _choice("back", "Back")]
        selected = _ask_select("Saved responses", choices)
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
    if source != "own":
        _check_response_limit(config)
    if source in {"generate", "enlarge", "fresh", "reference"}:
        reference = (
            None
            if source == "fresh"
            else _reference_prompt(config, required=source in {"enlarge", "reference"})
        )
        instructions = _ask_text("Extra instructions (optional, e.g. what to match in your CSV)")
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
            if kind == "csv":
                break
            selected = _select_saved_preview(config)
            if selected is not None:
                return selected
        path = _select_csv()
        profiles = _import_profiles(config, path)
        config.pool_file = str(path)
        mode = _ask_select(
            "How should rows be chosen from your CSV?",
            [
                _choice("ordered", "Use rows in order"),
                _choice("shuffle", "Shuffle rows (use each row once)"),
                _choice("sample", "Pick rows randomly (rows can repeat)"),
            ],
            default="ordered",
        )
        if getattr(config, "_prompt_csv_count", False):
            limit = min(config.max_runs, len(profiles)) if mode in {"ordered", "shuffle"} else config.max_runs
            if config.one_response:
                limit = 1
            config.count = _ask_number("Number of responses", min(config.count, limit), max_val=limit)
        if mode in {"ordered", "shuffle"} and config.count > len(profiles):
            _info(f"The CSV has {len(profiles)} rows. Choose a count within that range.")
            config.count = _ask_number(
                "Number of responses",
                min(config.count, len(profiles)),
                max_val=min(config.max_runs, len(profiles)),
            )
        _check_response_limit(config)
        return prepare_responses(config, profiles, sampling=mode)
    from .generator import CHOICE_TYPES, apply_reference_weights, make_answer
    from .routing import Routing

    routes = Routing(config)

    other = [q for q in config.questions if q.qtype not in CHOICE_TYPES and q.fixed_value is None]
    use_ai = False
    if other:
        _info(
            "Random selection supports multiple choice, checkboxes, dropdowns and numeric scales. This form also has text or date questions."
        )
        use_ai = _ask_confirm("Auto-generate answers for the other questions?")
        if not use_ai and not routes.enabled and any(q.required for q in other):
            _info(
                "Required written/date answers need AI or supplied fixed values. Optional answers may stay blank."
            )
            return None
    reference = _reference_prompt(config)
    instructions = _ask_text("Additional instructions for written answers (optional)") if use_ai else ""
    if reference:
        apply_reference_weights(config, reference)
    distribution = _ask_select(
        "How often should each choice be picked?",
        [
            _choice("auto", "Automatic (follow your CSV, or use equal chances)"),
            _choice("manual", "Set the chances myself"),
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
    while True:
        task = _ask_select(
            "Start",
            [
                _choice("new", "Create new responses"),
                _choice("saved", "Open saved responses"),
                _choice("generation", "Open saved AI work"),
                _choice("inspect", "View a form's questions"),
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
                        count = payload["request"]["count"]
                        status = "ready to save" if done == count else "responses ready"
                        jobs.append(
                            (path.stat().st_mtime, _choice(path, f"{path.name}: {done}/{count} {status}"))
                        )
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
            if not jobs:
                _info("No saved AI work found.")
                continue
            selected = _ask_select(
                "Saved AI work (newest first)",
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
    from .routing import Routing

    Routing(config).choose_route(
        {q.index: q.fixed_value for q in config.questions if q.fixed_value is not None}
    )
    config._prompt_csv_count = True
    while True:
        source = _ask_select(
            "Where should the answers come from?",
            [
                _choice("generate", "Generate answers with AI"),
                _choice("own", "Use my own answers"),
                _choice("random", "Pick answers randomly (best for choice questions)"),
            ],
            default="generate",
        )
        if source == "generate":
            source = _ask_select(
                "How should AI create the answers?",
                [
                    _choice("fresh", "Create new fictional responses"),
                    _choice("reference", "Follow an existing CSV's style and answer patterns"),
                    _choice("enlarge", "Create extra responses based on an existing CSV"),
                ],
                default="fresh",
            )
        if source != "own":
            config.count = _ask_number(
                "Number of additional responses" if source == "enlarge" else "Number of responses",
                max_val=1 if config.one_response else config.max_runs,
            )
        try:
            answers = _prepare_source(config, source)
        except FileSelectionCancelled:
            _info("No file chosen. Choose another way to get answers.")
            continue
        except ValueError as exc:
            _info(f"Could not prepare responses: {exc}")
            _info("Choose another way to get answers and try again.")
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


@click.command()
@click.argument("url")
@click.option("--save/--no-save", default=True)
@click.option("-o", "--output", default=DEFAULT_CONFIG_NAME)
@click.option("--browser", type=click.Choice(BROWSERS), default="headless", hidden=True)
def inspect(url, save, output, browser):
    """Display the whole form and optionally save its configuration."""
    _run_inspect(url, browser=browser, save=save, output=output)


@click.command()
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


@click.command("preview")
@click.argument("url", required=False)
@click.option("-c", "--config", "config_path")
@click.option("-n", "--count", default=None, type=click.IntRange(1, 500))
@click.option("--pool", type=click.Path(exists=True, dir_okay=False))
@click.option("--plan", type=click.Path(exists=True, dir_okay=False))
def _legacy_preview(url, config_path, count, pool, plan):
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


def _response_options(function):
    """Keep the fast and prepare-only commands on the same input contract."""
    options = [
        click.option("--sign-in", is_flag=True, help="Open a temporary browser for Google sign-in."),
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
        click.option("-m", "--message", default="", help="Extra instructions for ChatGPT."),
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
            help="Browser to use; defaults to saved settings or auto.",
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return click.argument("url")(function)


def _command_answers(
    url, count, message, like, data, browser, sign_in=False, date_order=None, *, cleanup_on_success=True
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
    inspection = {"display": False}
    if sign_in:
        inspection.update(sign_in=True, browser=browser or "auto")
    current = _run_inspect(url, **inspection)
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
        _check_response_limit(config, remaining)
        config.browser = browser or (config.browser if config.browser != "headless" else "auto")
        save_config(config, DEFAULT_CONFIG_NAME)
        return config, answers, clean_path(data), False
    if count is not None:
        config.count = count
    _check_response_limit(config)
    source = data or like
    if source and (
        not config.pool_file or clean_path(config.pool_file).resolve() != clean_path(source).resolve()
    ):
        config.pool_column_map = {}
        for question in config.questions:
            question.pool_column = None
    config.browser = browser or (config.browser if config.browser != "headless" else "auto")
    config.pool_file = str(clean_path(data).resolve()) if data else None
    validate_config(config)
    if data:
        answers = prepare_responses(config, load_reference(config, data), ordered=True)
    else:
        reference = load_reference(config, like) if like else None
        answers = _generate(config, reference, message, browser=config.browser, progress=None, compact=True)
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
@click.option("--halt", is_flag=True, help="Fill visible forms without submitting, and leave them open.")
def generate(url, count, message, like, data, browser, halt, sign_in=False, date_order=None):
    """Generate answers and submit them automatically. Use --halt to review instead."""
    from .artifacts import digest

    config, answers, path, owned = _command_answers(
        url, count, message, like, data, browser, sign_in, date_order
    )
    remaining = _track_plan(config, answers, path, digest(path), interactive=False)
    _apply_browser(config, config.browser, headless=not halt)
    finished = _run_fill(config, answers=remaining, submit=not halt, halt=halt, compact=True)
    _finish_plan(path, config._plan_digest, keep_files=not owned or finished is False)


@cli.command()
@_response_options
def prepare(url, count, message, like, data, browser, sign_in=False, date_order=None):
    """Create and save response JSON without filling or submitting the form."""
    _, _, path, _ = _command_answers(
        url, count, message, like, data, browser, sign_in, date_order, cleanup_on_success=False
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
                elif payload.get("kind") == "gformbot-generation":
                    count = payload["request"]["count"]
                    done = sum(row is not None for row in payload["responses"])
                    label = f"AI work | {done}/{count} responses ready"
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


@click.command()
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
        _manage_batch(config, answers, clean_path(plan), saved=True, keep_files=keep_files)
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


@click.command()
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
    table = Table(title="Saved responses", header_style="bold cyan")
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


@click.command()
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
    _info(f"AI responses saved: {len(valid)}/{len(rows)}.")
    if not resume and not export:
        choice = _ask_select(
            "Saved AI work",
            [
                _choice(
                    "resume",
                    "Continue creating the remaining responses"
                    if len(valid) < len(rows)
                    else "Save the completed responses for later",
                ),
                _choice("export", "Save completed responses as a CSV"),
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
    if len(valid) < len(rows):
        _info("Resuming through ChatGPT; the saved form, instructions and reference context will be sent.")
    else:
        _info("All responses are ready. Saving your answers without opening a browser.")
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
    from .selenium_runtime import PACKAGE

    for module in ("selenium", "tkinter", "pyperclip"):
        present = importlib.util.find_spec(PACKAGE if module == "selenium" else module) is not None
        table.add_row(
            module,
            ("Available (included)" if module == "selenium" and PACKAGE != "selenium" else "Available")
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
@click.option("--followups", is_flag=True, help="Show saved form instructions and links.")
def log(log_path, last, followups):
    """Show recent run results, including complete response data."""
    if followups:
        from .followups import display
        from .state import Journal

        display(Journal("").followups(log_path=log_path, last=last), sys.modules[__name__], full=True)
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
