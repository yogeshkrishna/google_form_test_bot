"""A short guided workflow with the same operations available as CLI commands."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import deque
import csv
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import click
from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import (DEFAULT_CONFIG_NAME, RunConfig, clean_path, config_from_dict,
                     config_from_schema, config_to_dict, load_config, save_config,
                     validate_config)

console = Console(highlight=False, safe_box=True)
_INQUIRER = sys.stdin.isatty() and sys.stdout.isatty()
BROWSERS = ["headless", "chrome", "chrome-background", "firefox", "edge", "auto"]


@dataclass
class MenuChoice:
    value: object
    name: str


def _choice(value, name):
    return MenuChoice(value, name)


def _ask_text(message, default=""):
    if _INQUIRER:
        value = inquirer.text(message=message, default=default).execute()
        if value is None:
            raise click.Abort()
        return value.strip()
    value = input(f"{message}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def _ask_confirm(message, default=False):
    if _INQUIRER:
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
        value = inquirer.select(message=message,
                                choices=[{"value": c.value, "name": c.name} for c in choices],
                                default=default).execute()
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
    console.print(Panel.fit(f"[bold cyan]gformbot[/bold cyan] [dim]{__version__}[/dim]\nPrepare, preview and fill Google Forms.", border_style="cyan"))


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
    from .validation import needs_email, date_format
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
        if q.page != previous_page:
            section = config.sections[q.page - 1] if q.page <= len(config.sections) else f"Section {q.page}"
            table.add_row("", Text(section, style="bold cyan"), "", "")
            previous_page = q.page
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
        table.add_row(str(q.index), Text(question), Text(q.qtype + ("\nRequired" if q.required else "\nOptional")), Text(details or "Free text"))
    console.print(table)
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
                selected = filedialog.askopenfilename(parent=root, title=title,
                    filetypes=[("CSV files", "*.csv")])
            finally:
                root.destroy()
        except (ImportError, RuntimeError) as exc:
            raise ValueError("The file picker needs a graphical desktop and Python Tk support. On Linux install python3-tk. Scripted commands accept --pool or --reference.") from exc
        except Exception as exc:
            raise ValueError(f"Cannot open the file picker: {exc}. Use a desktop session or a scripted --pool/--reference command.") from exc
        if not selected:
            raise FileSelectionCancelled()
        path = Path(selected)
        if path.is_file() and path.suffix.lower() == ".csv":
            return path.resolve()
        console.print("Choose an existing .csv file.")


def _mapping(config, headers):
    from .generator import map_columns
    try:
        return map_columns(config, headers)
    except ValueError as exc:
        console.print(Text(str(exc), style="yellow"))
    mapping = {}
    used = set()
    for q in config.questions:
        candidates = [h for h in headers if h.casefold().strip() in {q.text.casefold().strip(), f"q{q.index}", f"entry.{q.entry_id}"}]
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
    from .generator import read_pool, load_reference, CheckboxRepairError
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
            config.csv_date_order = _ask_select("Date order in this CSV", [
                _choice("mdy", "Month/day/year (e.g. 10/15/2023)"),
                _choice("dmy", "Day/month/year (e.g. 15/10/2023)")])
            continue
        except CheckboxRepairError as exc:
            if repair:
                raise
            console.print(Text(str(exc), style="yellow"))
            if not _ask_confirm("Explicitly repair checkbox duplicates, exclusive choices and excess selections? Changes will be recorded."):
                raise click.Abort()
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
    from .ai import generate_responses
    _info("Opening ChatGPT in a private browser. The form, instructions, and reference examples will be sent to ChatGPT.")
    return generate_responses(config, config.count, reference=reference, instructions=instructions,
        progress=lambda done, total: _info(f"Generating responses: {done}/{total}"),
        attention=_chatgpt_attention if sys.stdin.isatty() else None, **options)


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
    payload = {"schema_version": 1, "kind": "gformbot-run", "created_at": datetime.now().astimezone().isoformat(),
               "form_fingerprint": schema_fingerprint(config), "config": config_to_dict(config),
               "source": source, "repairs": getattr(config, "_repairs", []), "responses": answers,
               "cleanup_on_success": cleanup_on_success,
               "temporary_files": describe(artifacts if artifacts is not None else getattr(config, "_generated_artifacts", []), target.parent)}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    from .previews import remember
    try:
        remember(target)
    except OSError as exc:
        _info(f"Preview saved, but could not remember its location: {exc}. Use the run command below.")
    _info(f"Exact response plan: {target.resolve()}")
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
    _info("Or start gformbot, open the same form, choose Use own data > Saved preview, then pick this batch from the newest-first list.")


def _next_action(saved=False):
    choices = [] if saved else [_choice("preview", "Save preview and exit")]
    return _ask_select("What next?", choices + [
        _choice("fill", "Fill for review (you decide when to submit)"),
        _choice("submit", "Fill and submit")], default="fill")


def _choose_browser():
    return _ask_select("Browser", [
        _choice("chrome-background", "Chrome (background; opens only when you choose, recommended)"),
        _choice("chrome", "Chrome (always visible)"),
        _choice("headless", "Headless Chrome (invisible)"),
        _choice("firefox", "Firefox (visible)"),
        _choice("edge", "Edge (visible)"),
        _choice("auto", "Auto-detect default browser")], default="chrome-background")


def _apply_browser(config, browser=None, headless=None):
    if browser is not None:
        config.background = browser == "chrome-background"
        config.browser = "chrome" if config.background else browser
        config.headless = config.browser == "headless"
    if headless is not None:
        config.headless = headless
        if not headless and config.browser == "headless":
            config.browser = "chrome"
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
        if not isinstance(submitted, list) or any(type(i) is not int or not 1 <= i <= config.count for i in submitted) or len(set(submitted)) != len(submitted):
            raise ValueError("The saved submission progress is invalid.")
        config._submitted_indices = submitted
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot load response plan {path}: {exc}") from exc
    return config, answers


def _track_plan(config, answers, path, expected_digest):
    """Persist confirmed progress so an interrupted batch can resume safely."""
    from .artifacts import digest
    path = clean_path(path)
    config._plan_digest = digest(path)
    if config._plan_digest != expected_digest:
        raise ValueError("The saved preview changed while you were reviewing it. Reopen it before running.")
    completed = set(getattr(config, "_submitted_indices", []))
    remaining = [i for i in range(1, len(answers) + 1) if i not in completed]
    if completed:
        _info(f"{len(completed)} response(s) already confirmed. Continuing only the {len(remaining)} remaining response(s).")
        if not remaining:
            raise ValueError("Every response in this preview was already confirmed submitted. Prepare a new batch to avoid duplicates.")
    config.count = len(remaining)
    def record(index):
        if digest(path) != config._plan_digest:
            raise ValueError("The saved preview changed during execution. Submission was confirmed; check the log before retrying.")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        completed.add(remaining[index - 1])
        payload["submitted_indices"] = sorted(completed)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        config._plan_digest = digest(path)
    config._record_submission = record
    return [answers[i - 1] for i in remaining]


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


def _review_mode(config, submit):
    if submit or config.headless or config.browser == "headless" or not sys.stdin.isatty():
        return "check"
    return _ask_select("How should prepared responses stay open?", [
        _choice("wait", "One at a time: open each when ready, submit before the next"),
        _choice("tabs", "Prepare all in the background; open chosen responses when ready" if config.background else
                "Separate private tabs: prepare all, then let me submit each")], default="wait")


def _background_review(config, pending, answers):
    """Only create a visible browser in response to the user's Open action."""
    from .browser import get_driver
    from .filler import submit_response, _positive_confirmation
    from .logger import log_result
    from selenium.common.exceptions import WebDriverException
    viewer = None
    opened = {}
    all_confirmed = True
    def check():
        nonlocal all_confirmed
        if viewer is None:
            return
        handles = viewer.window_handles
        for handle, index in list(opened.items()):
            if handle not in handles:
                _info(f"Response {index}: review tab closed before confirmation. Check Google Forms before reopening it.")
                del opened[handle]
                all_confirmed = False
                continue
            viewer.switch_to.window(handle)
            if _positive_confirmation(viewer, config.confirmation_text, config.url):
                log_result(config.log_file, index, answers[index - 1], True, "submitted manually")
                if getattr(config, "_record_submission", None):
                    config._record_submission(index)
                pending.pop(index, None)
                del opened[handle]
                _info(f"Response {index}: manual submission confirmed.")
    try:
        while pending:
            choice = _ask_select(f"{len(pending)} response(s) ready in the background", [
                _choice("open", "Open a response to review, edit or submit"),
                _choice("check", "Check confirmations for responses I opened"),
                _choice("stop", "Exit and keep unsubmitted responses saved")], default="open")
            if choice == "stop":
                check()
                return not pending and all_confirmed
            if choice == "check":
                check()
                if pending:
                    _info(f"{len(pending)} response(s) still unconfirmed. Nothing opens automatically.")
                continue
            index = next(iter(pending)) if len(pending) == 1 else _ask_select("Which response should open?", [
                _choice(i, f"Response {i}" + (" (already open; keep my edits)" if i in opened.values() else ""))
                for i in pending] + [_choice("back", "Back")])
            if index == "back":
                continue
            if viewer is None:
                viewer = get_driver(config.browser, headless=False)
            existing = next((h for h, i in opened.items() if i == index), None)
            if existing:
                viewer.switch_to.window(existing)
            else:
                if opened:
                    viewer.switch_to.new_window("tab")
                _info(f"Opening response {index}. You can edit it; the saved preview keeps the prepared answers.")
                ok, message = submit_response(config, answers[index - 1], submit=False, driver=viewer)
                if not ok:
                    raise ValueError(f"Could not open response {index} for review: {message}")
                viewer.execute_script("document.title = arguments[0]", f"Response {index}/{len(answers)} — {config.form_title}")
                opened[viewer.current_window_handle] = index
            if len(pending) == 1:
                before = dict(opened)
                confirmed = _wait_for_manual_submissions(viewer, config, opened, answers)
                for handle, i in before.items():
                    if handle not in opened:
                        pending.pop(i, None)
                return confirmed and all_confirmed
        return all_confirmed
    except WebDriverException as exc:
        _info(f"Review browser closed or disconnected. Keeping unconfirmed responses. {type(exc).__name__}")
        return False
    finally:
        if viewer is not None:
            try:
                viewer.quit()
            except Exception:
                pass


def _wait_for_manual_submissions(driver, config, pending, answers, *, tabs=False):
    from .filler import _positive_confirmation
    from .logger import log_result
    from selenium.common.exceptions import NoSuchWindowException, WebDriverException
    _info("Submit in the browser when ready. There is no time limit. Ctrl+C opens stop/continue options.")
    all_confirmed = True
    while pending:
        try:
            if tabs:
                _ask_text(f"{len(pending)} response(s) awaiting confirmation. Review/submit the tabs, then press Enter to check (Ctrl+C for stop options)")
            handles = driver.window_handles
            missing = False
            for handle, index in list(pending.items()):
                if handle not in handles:
                    _info(f"Response {index}: tab closed before submission could be confirmed. Keeping the saved preview.")
                    missing = True
                    del pending[handle]
                    continue
                if tabs:
                    driver.switch_to.window(handle)
                if _positive_confirmation(driver, config.confirmation_text, config.url):
                    log_result(config.log_file, index, answers[index - 1], True, "submitted manually")
                    record = getattr(config, "_record_submission", None)
                    if record:
                        record(index)
                    del pending[handle]
                    _info(f"Response {index}: manual submission confirmed.")
            if missing:
                all_confirmed = False
                if not tabs:
                    return False
            if tabs and pending:
                driver.switch_to.window(next(iter(pending)))
            if pending:
                time.sleep(0.5)
        except KeyboardInterrupt:
            choice = _ask_select("Unsubmitted responses are still open", [
                _choice("wait", "Keep waiting for me to submit"),
                _choice("stop", "Close the browser and keep the saved preview")], default="wait")
            if choice == "stop":
                return False
        except (NoSuchWindowException, WebDriverException) as exc:
            _info(f"Browser closed or disconnected; keeping the saved preview. {type(exc).__name__}")
            return False
    return all_confirmed


def _run_fill(config, dry_run=False, *, answers=None, submit=False):
    from .browser import get_driver
    from .filler import submit_response
    from .generator import prepare_responses, validate_answer
    from .inspector import inspect_form, schema_fingerprint
    from .logger import log_result, validate_log_path
    validate_config(config)
    answers = prepare_responses(config) if answers is None else [validate_answer(config, a) for a in answers]
    if len(answers) != config.count:
        raise ValueError("Response count does not match prepared answers.")
    if dry_run:
        _info("Preview only; no browser was opened.")
        return
    validate_log_path(config.log_file)
    current = config_from_schema(inspect_form(config.url))
    if schema_fingerprint(current) != schema_fingerprint(config):
        raise ValueError("The form has changed since inspection. Inspect it and prepare a new response plan.")
    completed = 0
    mode = "submitted" if submit else "filled"
    review = _review_mode(config, submit)
    if review == "tabs" and not config.background and config.count > 20:
        if not _ask_confirm(f"Open {config.count} private tabs? This may use considerable memory. Choose No to review one at a time."):
            review = "wait"
    _section("Submitting responses" if submit else "Filling without submitting")
    if not submit:
        _info("The tool will not click Submit. You can review and submit responses yourself in the browser.")
        if config.headless or config.browser == "headless":
            _info("The browser is invisible. Choose Chrome or --browser chrome to inspect a rehearsal.")
    driver = get_driver(config.browser, headless=config.headless or config.background)
    pending = {}
    all_reviewed = True
    try:
        if config.background:
            _info("Filling in the background. No browser window opens unless you choose Open a response.")
        for index, answer in enumerate(answers, 1):
            logged = False
            try:
                if review == "tabs" and not config.background and index > 1:
                    driver.switch_to.new_window("tab")
                ok, message = submit_response(config, answer, submit=submit, driver=driver)
                if ok:
                    completed += 1
                log_result(config.log_file, index, answer, ok, message)
                logged = True
                if ok and submit and getattr(config, "_record_submission", None):
                    config._record_submission(index)
                if not ok:
                    raise ValueError(message)
                console.print(Text(f"[{index}/{len(answers)}] {mode}", style="green"))
                if review != "check":
                    driver.execute_script("document.title = arguments[0]", f"Response {index}/{len(answers)} — {config.form_title}")
                    pending[index if config.background else driver.current_window_handle] = index
                    if review == "wait":
                        all_reviewed = (_background_review(config, pending, answers) if config.background else
                                        _wait_for_manual_submissions(driver, config, pending, answers))
                        if not all_reviewed:
                            break
            except KeyboardInterrupt:
                _info(f"Stopped: {completed}/{len(answers)} responses {mode}.")
                raise
            except Exception as exc:
                if not logged:
                    try:
                        log_result(config.log_file, index, answer, False, str(exc))
                    except (OSError, ValueError) as log_error:
                        console.print(Text(f"Could not write run log: {log_error}", style="yellow"))
                raise ValueError(f"Response {index} failed after {completed}/{len(answers)} {mode}: {exc}") from exc
            if index < len(answers) and config.max_delay:
                import random
                time.sleep(random.uniform(config.min_delay, config.max_delay))
        if review == "tabs":
            if config.background:
                all_reviewed = _background_review(config, pending, answers)
            else:
                if pending:
                    driver.switch_to.window(next(iter(pending)))
                all_reviewed = _wait_for_manual_submissions(driver, config, pending, answers, tabs=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass  # The user may already have closed the browser.
    _info(f"Finished: {completed}/{len(answers)} responses {mode}. Log: {clean_path(config.log_file).resolve()}")
    if not submit and review == "check":
        _info("No responses were submitted. Use Fill and submit or --submit to record them in Google Forms.")
    if not all_reviewed:
        _info("Stopped with unconfirmed responses. The saved preview is kept; reopening it skips confirmed submissions. If you closed a submitted tab before confirmation, check Google Forms before retrying that response.")
    return all_reviewed


def _confirm_submit(config, submit, yes=False):
    if not submit or yes:
        return
    if not sys.stdin.isatty():
        raise ValueError("Submission needs confirmation. Use --submit --yes only when you intend to submit this exact plan.")
    if not _ask_confirm(f"Submit these {config.count} responses to {config.form_title}?"):
        raise click.Abort()


def _validate_execution_flags(dry_run, submit, yes):
    if dry_run and submit:
        raise ValueError("Choose --dry-run to save a preview, or --submit to submit; these cannot be combined.")
    if yes and not submit:
        raise ValueError("--yes only confirms submission. Add --submit if you intend to submit, or remove --yes for review.")


@dataclass
class SavedPreview:
    path: Path
    config: RunConfig
    answers: list


def _select_saved_preview(config):
    from .previews import available
    from .inspector import schema_fingerprint
    while True:
        matches = []
        unreadable = 0
        for path in available():
            try:
                saved, answers = _load_plan(path)
                if schema_fingerprint(saved) != schema_fingerprint(config):
                    continue
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                created = datetime.fromisoformat(payload["created_at"]).timestamp()
                matches.append((created, SavedPreview(path, saved, answers)))
            except (ValueError, OSError, KeyError, TypeError, OverflowError):
                unreadable += 1
        matches.sort(key=lambda item: (item[0], str(item[1].path)), reverse=True)
        _info("Saved previews for this form, newest first. Each reuses its exact answers and response count.")
        if unreadable:
            _info(f"Skipped {unreadable} unreadable or unrelated JSON file(s).")
        choices = [_choice(batch, f"{datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M')} | {batch.config.count - len(batch.config._submitted_indices)} remaining / {batch.config.count} total | {batch.path}")
                   for created, batch in matches]
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
            if schema_fingerprint(saved) != schema_fingerprint(config):
                raise ValueError("This preview now describes another form.")
            return SavedPreview(selected.path, saved, answers)
        except ValueError as exc:
            _info(str(exc))


def _prepare_source(config, source):
    if source in {"generate", "enlarge"}:
        reference = _reference_prompt(config, required=source == "enlarge")
        instructions = _ask_text("Additional instructions (optional; say what to match in reference data)")
        if source == "enlarge":
            instructions = "Generate additional fictional profiles; avoid merely copying entire reference rows. " + instructions
        answers = _generate(config, reference, instructions)
        from .generator import write_csv
        output = _available_path("gformbot-additional.csv" if source == "enlarge" else "gformbot-generated.csv")
        write_csv(config, answers, output)
        config._generated_artifacts = [output]
        _info(f"CSV saved: {output.resolve()}")
        return answers
    if source == "own":
        from .generator import prepare_responses
        while True:
            kind = _ask_select("Use own data", [
                _choice("csv", "Import a CSV (opens file picker after Enter)"),
                _choice("saved", "Saved preview (newest first; no file picker)"),
                _choice("back", "Back to response sources")], default="csv")
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
        mode = _ask_select("Use complete CSV rows", [
            _choice("ordered", "Original order"), _choice("sample", "Sample whole rows")], default="ordered")
        if getattr(config, "_prompt_csv_count", False):
            limit = min(config.max_runs, len(profiles)) if mode == "ordered" else config.max_runs
            config.count = _ask_number("Number of responses", min(config.count, limit), max_val=limit)
        if mode == "ordered" and config.count > len(profiles):
            _info(f"The CSV has {len(profiles)} rows. Choose a count within that range.")
            config.count = _ask_number("Number of responses", min(config.count, len(profiles)), max_val=min(config.max_runs, len(profiles)))
        return prepare_responses(config, profiles, ordered=mode == "ordered")
    from .generator import CHOICE_TYPES, apply_reference_weights, _random_from_options
    other = [q for q in config.questions if q.qtype not in CHOICE_TYPES and q.fixed_value is None]
    use_ai = False
    if other:
        _info("Random selection supports multiple choice, checkboxes, dropdowns and numeric scales. This form also has text or date questions.")
        use_ai = _ask_confirm("Auto-generate answers for the other questions?")
        if not use_ai:
            return None
    reference = _reference_prompt(config)
    instructions = _ask_text("Additional instructions for written answers (optional)") if use_ai else ""
    if reference:
        apply_reference_weights(config, reference)
    distribution = _ask_select("Choice-answer distribution", [
        _choice("auto", "Automatic (reference frequencies if supplied; otherwise uniform)"),
        _choice("manual", "Set frequencies manually")], default="auto")
    if distribution == "manual":
        _configure_weights(config)
    validate_config(config)
    import random
    partials = []
    for _ in range(config.count):
        reference_row = random.choice(reference) if reference and distribution == "auto" else None
        partials.append({q.index: q.fixed_value if q.fixed_value is not None else
                         reference_row[q.index] if reference_row is not None else _random_from_options(q)
                         for q in config.questions if q.fixed_value is not None or q.qtype in CHOICE_TYPES})
    if use_ai:
        from .ai import complete_responses
        _info("Opening ChatGPT in a private browser to complete the remaining answers using the form and reference examples.")
        return complete_responses(config, partials, reference=reference, instructions=instructions,
            progress=lambda done, total: _info(f"Preparing responses: {done}/{total}"),
            attention=_chatgpt_attention if sys.stdin.isatty() else None)
    from .generator import validate_answer
    return [validate_answer(config, row) for row in partials]


def _wizard():
    _print_banner()
    _info("Arrow keys to choose; Enter to select; Ctrl+C to cancel.")
    config = _run_inspect(_url_prompt())
    if config.unsupported:
        raise ValueError("This form contains unsupported questions: " + "; ".join(config.unsupported))
    config._prompt_csv_count = True
    while True:
        source = _ask_select("Response source", [
            _choice("generate", "Generate responses (recommended)"),
            _choice("own", "Use own data"),
            _choice("enlarge", "Enlarge existing responses"),
            _choice("random", "Random (recommended for choice questions)")], default="generate")
        if source != "own":
            config.count = _ask_number("Number of responses", max_val=config.max_runs)
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
    from .artifacts import digest
    plan_digest = digest(target)
    _show_preview(config, answers)
    action = _next_action(saved=True) if reusing_preview else _next_action()
    if action == "preview":
        _preview_instructions(target)
        return
    answers = _track_plan(config, answers, target, plan_digest)
    _apply_browser(config, _choose_browser())
    _confirm_submit(config, action == "submit")
    save_config(config, DEFAULT_CONFIG_NAME)
    finished = _run_fill(config, answers=answers, submit=action == "submit")
    _finish_plan(target, config._plan_digest, keep_files=finished is False)

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
            raise ValueError("This config predates reliable form inspection. Run gformbot inspect URL to refresh it.")
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
@click.option("--repair", is_flag=True, help="Explicitly repair and record supported checkbox issues.")
@click.option("--dry-run", is_flag=True, help="Save and preview answers without opening a browser.")
@click.option("--submit", is_flag=True, help="Submit after filling; otherwise stop before Submit.")
@click.option("--yes", is_flag=True, help="Confirm --submit for scripts.")
@click.option("--keep-files", is_flag=True, help="Keep the response plan after a successful run.")
@click.option("--date-order", type=click.Choice(["mdy", "dmy"]), help="Resolve ambiguous numeric dates in CSV input.")
def fill(url, config_path, count, pool, browser, headless, ordered, repair, dry_run, submit, yes, keep_files, date_order):
    """Prepare exact answers, then fill the form; submission is opt-in."""
    _validate_execution_flags(dry_run, submit, yes)
    from .generator import load_reference, prepare_responses
    config = _resolve_config(url, config_path, pool, browser, headless, count)
    if date_order:
        config.csv_date_order = date_order
    if browser is None and headless is None and config.headless:
        config.browser, config.headless = "chrome", False
    changes = []
    profiles = load_reference(config, config.pool_file, repair=repair, changes=changes) if config.pool_file else None
    config._repairs = changes
    answers = prepare_responses(config, profiles, ordered=ordered)
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
@click.option("-n", "--count", default=5, type=click.IntRange(1, 500))
@click.option("--pool", type=click.Path(exists=True, dir_okay=False))
@click.option("--plan", type=click.Path(exists=True, dir_okay=False))
def preview(url, config_path, count, pool, plan):
    """Preview a saved plan, or prepare and save a fresh batch."""
    from .generator import prepare_responses
    if plan:
        config, answers = _load_plan(plan)
    else:
        config = _resolve_config(url, config_path, pool=pool, count=count)
        answers = prepare_responses(config)
        target = _save_plan(config, answers)
    _show_preview(config, answers)
    _preview_instructions(plan or target)


@cli.command()
@click.argument("url")
@click.option("-n", "--count", default=10, type=click.IntRange(1, 500))
@click.option("--reference", type=click.Path(exists=True, dir_okay=False))
@click.option("--instructions", default="")
@click.option("--browser", type=click.Choice(["chrome", "firefox", "edge", "auto"]), default="chrome", show_default=True)
@click.option("--date-order", type=click.Choice(["mdy", "dmy"]), help="Resolve ambiguous numeric dates in the reference CSV.")
@click.option("-o", "--output", default="gformbot-generated.csv")
def generate(url, count, reference, instructions, browser, date_order, output):
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
    profiles = load_reference(config, reference) if reference else None
    answers = _generate(config, profiles, instructions, browser=browser)
    write_csv(config, answers, target)
    plan = _save_plan(config, answers, target.with_suffix(".json"), source="generate", cleanup_on_success=False, artifacts=[target])
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
    _show_preview(config, answers)
    interactive = sys.stdin.isatty() and not (dry_run or submit or yes or browser is not None or headless is not None)
    if interactive:
        action = _next_action(saved=True)
        if action == "preview":
            _preview_instructions(plan)
            return
        submit = action == "submit"
        browser = _choose_browser()
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
        raise SystemExit(130)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)
    except (ValueError, OSError) as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        raise SystemExit(1)
    except Exception as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        raise SystemExit(1)
