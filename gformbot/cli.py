"""
gformbot CLI — main entry point.

Default (no args)    → polished interactive wizard
gformbot fill        → fill a form using a config or flags
gformbot inspect     → inspect a form and generate a config
gformbot config      → open/edit the config file
gformbot preview     → dry-run preview without submitting
gformbot log         → show the run log
gformbot version     → show version
"""

from __future__ import annotations

import sys
import os
import re
import subprocess
import platform
from pathlib import Path

import click

from gformbot import __version__
from gformbot.config import (
    DEFAULT_CONFIG_NAME,
    RunConfig,
    load_config,
    save_config,
    config_from_schema,
)


# ---------------------------------------------------------------------------
# Rich / fallback terminal styling
# ---------------------------------------------------------------------------

import sys as _sys

# Detect if the terminal supports Unicode by actually trying to encode
def _check_unicode_safe() -> bool:
    try:
        enc = _sys.stdout.encoding or "ascii"
        "\u25c8\u2714\u2718\u26a0\u2139".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False

_UNICODE_SAFE = _check_unicode_safe()

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich import print as rprint
    import rich.traceback
    # Only use Rich if the terminal can render its output (Unicode + color)
    _RICH = _UNICODE_SAFE
    if _RICH:
        rich.traceback.install(show_locals=False)
        console = Console(highlight=False, safe_box=True)
    else:
        console = None
except ImportError:
    _RICH = False
    console = None

try:
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice
    _INQUIRER = True
except ImportError:
    _INQUIRER = False


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------

BRAND = "gformbot"
ACCENT = "cyan"

# Symbol set — fall back to ASCII when Unicode isn't safe
_SYM_DIAMOND  = "*" if not _UNICODE_SAFE else "\u25c8"   # ◈
_SYM_CHECK    = "OK" if not _UNICODE_SAFE else "\u2714"  # ✔
_SYM_WARN     = "!" if not _UNICODE_SAFE else "\u26a0"   # ⚠
_SYM_CROSS    = "X" if not _UNICODE_SAFE else "\u2718"   # ✘
_SYM_INFO     = "i" if not _UNICODE_SAFE else "\u2139"   # ℹ


def _print_banner():
    if _RICH:
        console.print(Panel.fit(
            f"[bold {ACCENT}]{_SYM_DIAMOND}  gformbot[/bold {ACCENT}]  [dim]v{__version__}[/dim]\n"
            "[dim]General-purpose Google Form auto-filler[/dim]",
            border_style=ACCENT,
            padding=(0, 2),
        ))
    else:
        print(f"\n  {_SYM_DIAMOND}  gformbot  v{__version__}")
        print("  General-purpose Google Form auto-filler\n")


def _info(msg: str):
    if _RICH:
        console.print(f"[{ACCENT}]{_SYM_INFO}[/{ACCENT}]  {msg}")
    else:
        print(f"  {_SYM_INFO}  {msg}")


def _success(msg: str):
    if _RICH:
        console.print(f"[bold green]{_SYM_CHECK}[/bold green]  {msg}")
    else:
        print(f"  {_SYM_CHECK}  {msg}")


def _warn(msg: str):
    if _RICH:
        console.print(f"[bold yellow]{_SYM_WARN}[/bold yellow]  {msg}")
    else:
        print(f"  {_SYM_WARN}  {msg}")


def _error(msg: str):
    if _RICH:
        console.print(f"[bold red]{_SYM_CROSS}[/bold red]  {msg}")
    else:
        print(f"  {_SYM_CROSS}  {msg}", file=sys.stderr)


def _section(title: str):
    if _RICH:
        console.rule(f"[bold]{title}[/bold]")
    else:
        print(f"\n─── {title} ───")


# ---------------------------------------------------------------------------
# Wizard helpers (InquirerPy with plain-input fallback)
# ---------------------------------------------------------------------------

def _ask_text(message: str, default: str = "") -> str:
    if _INQUIRER:
        return inquirer.text(message=message, default=default).execute()
    val = input(f"  {message} [{default}]: ").strip()
    return val or default


def _ask_confirm(message: str, default: bool = True) -> bool:
    if _INQUIRER:
        return inquirer.confirm(message=message, default=default).execute()
    d = "Y/n" if default else "y/N"
    val = input(f"  {message} [{d}]: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _ask_select(message: str, choices: list, default=None):
    if _INQUIRER:
        from InquirerPy.base.control import Choice as C
        ch = [C(c, name=str(c)) if not isinstance(c, C) else c for c in choices]
        return inquirer.select(message=message, choices=ch, default=default).execute()
    # Plain fallback
    print(f"\n  {message}")
    for i, c in enumerate(choices, 1):
        label = c.name if hasattr(c, "name") else str(c)
        val = c.value if hasattr(c, "value") else c
        marker = " (default)" if val == default else ""
        print(f"    {i}. {label}{marker}")
    raw = input("  Enter number: ").strip()
    try:
        idx = int(raw) - 1
        c = choices[idx]
        return c.value if hasattr(c, "value") else c
    except (ValueError, IndexError):
        return default


def _ask_number(message: str, default: int, min_val: int = 1, max_val: int = 9999) -> int:
    if _INQUIRER:
        return inquirer.number(
            message=message,
            default=default,
            min_allowed=min_val,
            max_allowed=max_val,
        ).execute()
    while True:
        raw = input(f"  {message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            v = int(raw)
            if min_val <= v <= max_val:
                return v
            print(f"  Please enter a number between {min_val} and {max_val}.")
        except ValueError:
            print("  Please enter a valid number.")


# ---------------------------------------------------------------------------
# Clipboard detection
# ---------------------------------------------------------------------------

def _clipboard_url() -> str | None:
    """Try to read a Google Form URL from the clipboard."""
    try:
        import pyperclip  # optional dep
        val = pyperclip.paste().strip()
        if "docs.google.com/forms" in val and val.startswith("http"):
            return val
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name=BRAND)
@click.pass_context
def cli(ctx):
    """
    \b
    [*] gformbot  --  General-purpose Google Form auto-filler

    Run with no arguments to launch the interactive wizard.
    Use subcommands for scripted/power-user workflows.
    """
    if ctx.invoked_subcommand is None:
        _wizard()


# ---------------------------------------------------------------------------
# Subcommand: inspect
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url", required=False)
@click.option("--browser", default="auto", show_default=True,
              type=click.Choice(["auto", "chrome", "firefox", "edge", "headless"]),
              help="Browser to use for inspection.")
@click.option("--save/--no-save", default=True, show_default=True,
              help="Save the generated config to gformbot_config.yaml.")
@click.option("-o", "--output", default=DEFAULT_CONFIG_NAME, show_default=True,
              help="Output config file path.")
def inspect(url, browser, save, output):
    """Inspect a Google Form URL and generate a config file."""
    _print_banner()

    if not url:
        detected = _clipboard_url()
        if detected:
            _info(f"Detected URL in clipboard: {detected}")
            if _ask_confirm("Use this URL?"):
                url = detected
        if not url:
            url = _ask_text("Google Form URL")

    if not url:
        _error("No URL provided.")
        raise SystemExit(1)

    _run_inspect(url, browser, save, output)


def _run_inspect(url: str, browser: str, save: bool, output: str):
    from gformbot.inspector import inspect_form

    _section("Inspecting Form")
    schema = inspect_form(url, browser=browser, headless=True,
                          status_cb=_info)

    _section("Form Summary")
    if _RICH:
        t = Table(show_header=True, header_style=f"bold {ACCENT}")
        t.add_column("Q#", style="dim", width=4)
        t.add_column("Question", min_width=40)
        t.add_column("Type", width=10)
        t.add_column("Options", width=6)
        for q in schema.questions:
            t.add_row(
                str(q.index),
                q.text[:70] + ("…" if len(q.text) > 70 else ""),
                q.qtype,
                str(len(q.options)) if q.options else "—",
            )
        console.print(t)
    else:
        for q in schema.questions:
            print(f"  Q{q.index}: [{q.qtype}] {q.text[:60]}")

    config = config_from_schema(schema)

    if save:
        out_path = Path(output)
        save_config(config, out_path)
        _success(f"Config saved → {out_path.resolve()}")
        _info("Edit the YAML to set pool_file, weights, biases, etc.")

    return config


# ---------------------------------------------------------------------------
# Subcommand: fill
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url", required=False)
@click.option("-c", "--config", "config_path", default=None,
              help="Path to gformbot config YAML. Auto-detected if omitted.")
@click.option("-n", "--count", default=None, type=int,
              help="Number of responses to submit.")
@click.option("--pool", default=None,
              help="Path to response pool CSV.")
@click.option("--browser", default=None,
              type=click.Choice(["auto", "chrome", "firefox", "edge", "headless"]),
              help="Browser override.")
@click.option("--headless/--no-headless", default=None,
              help="Force headless mode on/off.")
@click.option("--dry-run", is_flag=True,
              help="Preview responses without submitting.")
def fill(url, config_path, count, pool, browser, headless, dry_run):
    """Fill and submit a Google Form."""
    _print_banner()
    config = _resolve_config(url, config_path, pool, browser, headless, count)
    _run_fill(config, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Subcommand: preview
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url", required=False)
@click.option("-c", "--config", "config_path", default=None)
@click.option("-n", "--count", default=5, show_default=True,
              help="Number of sample responses to preview.")
@click.option("--pool", default=None)
def preview(url, config_path, count, pool):
    """Preview generated responses without submitting."""
    _print_banner()
    config = _resolve_config(url, config_path, pool, None, None, count)
    _run_fill(config, dry_run=True)


# ---------------------------------------------------------------------------
# Subcommand: config
# ---------------------------------------------------------------------------

@cli.command("config")
@click.option("--show", is_flag=True, help="Print the config to terminal.")
@click.option("--edit", is_flag=True, help="Open the config in your default editor.")
@click.argument("config_path", default=DEFAULT_CONFIG_NAME, required=False)
def config_cmd(show, edit, config_path):
    """View or edit the gformbot config file."""
    p = Path(config_path)
    if not p.exists():
        _error(f"Config not found: {p}. Run `gformbot inspect <url>` first.")
        raise SystemExit(1)

    if show:
        if _RICH:
            from rich.syntax import Syntax
            syntax = Syntax(p.read_text(encoding="utf-8"), "yaml",
                           theme="monokai", line_numbers=True)
            console.print(syntax)
        else:
            print(p.read_text(encoding="utf-8"))
        return

    if edit:
        _open_editor(p)
        return

    # Default: show
    if _RICH:
        from rich.syntax import Syntax
        console.print(Syntax(p.read_text(encoding="utf-8"), "yaml",
                             theme="monokai", line_numbers=True))
    else:
        print(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Subcommand: log
# ---------------------------------------------------------------------------

@cli.command()
@click.option("-n", "--last", default=20, show_default=True,
              help="Show last N entries.")
@click.argument("log_path", default="gformbot_log.csv", required=False)
def log(last, log_path):
    """Display the submission log."""
    import csv as _csv
    p = Path(log_path)
    if not p.exists():
        _warn("No log file found yet.")
        return

    with p.open(encoding="utf-8") as f:
        rows = list(_csv.reader(f))

    if not rows:
        _warn("Log file is empty.")
        return

    header, data = rows[0], rows[1:]
    data = data[-last:]

    if _RICH:
        t = Table(show_header=True, header_style=f"bold {ACCENT}")
        for col in header:
            t.add_column(col)
        for row in data:
            style = "green" if len(row) > 2 and row[2] == "OK" else "red"
            t.add_row(*row, style=style)
        console.print(t)
    else:
        print("\t".join(header))
        for row in data:
            print("\t".join(row))


# ---------------------------------------------------------------------------
# Wizard (default mode)
# ---------------------------------------------------------------------------

def _wizard():
    """Full interactive wizard — the default when no subcommand is given."""
    _print_banner()

    if _RICH:
        console.print(
            "\n[dim]Welcome! This wizard will guide you through filling any Google Form.[/dim]\n"
            "[dim]Press [/dim][bold]Ctrl+C[/bold][dim] at any time to exit.\n[/dim]"
        )
    else:
        print("\n  Welcome! Press Ctrl+C at any time to exit.\n")

    # ── Step 1: URL ─────────────────────────────────────────────────────────
    _section("Step 1 — Form URL")

    detected_url = _clipboard_url()
    url = ""
    if detected_url:
        _info(f"Detected in clipboard: [link]{detected_url}[/link]" if _RICH else
              f"  Detected in clipboard: {detected_url}")
        if _ask_confirm("Use this URL?"):
            url = detected_url

    if not url:
        url = _ask_text("Paste the Google Form URL")

    if not url or "docs.google.com/forms" not in url:
        _error("That doesn't look like a Google Form URL. Please try again.")
        raise SystemExit(1)

    # ── Step 2: Config ───────────────────────────────────────────────────────
    _section("Step 2 — Config")

    config_path = Path(DEFAULT_CONFIG_NAME)
    existing_config = config_path.exists()
    config: RunConfig | None = None

    if existing_config:
        try:
            existing = load_config(config_path)
            if existing.url == url:
                _success(f"Found existing config for this form: {config_path}")
                use_existing = _ask_confirm("Use existing config?", default=True)
                if use_existing:
                    config = existing
            else:
                _warn(f"Existing config is for a different URL ({existing.url[:60]}…)")
                overwrite = _ask_confirm("Inspect the new URL and overwrite config?")
                if not overwrite:
                    _error("Aborted.")
                    raise SystemExit(0)
        except Exception as e:
            _warn(f"Could not load existing config ({e}). Will re-inspect.")

    if config is None:
        _info("Inspecting the form (this opens a hidden browser for ~10 seconds)…")
        try:
            config = _run_inspect(url, browser="headless", save=True,
                                  output=str(config_path))
        except Exception as e:
            _error(f"Inspection failed: {e}")
            _warn("You may need to check your internet connection or browser drivers.")
            raise SystemExit(1)

    # ── Step 3: Pool CSV ─────────────────────────────────────────────────────
    _section("Step 3 — Response Pool (optional)")

    _info("A CSV of real responses makes submissions more varied and realistic.")
    _info("The columns just need to loosely match the form questions.")

    if config.pool_file:
        _info(f"Current pool: {config.pool_file}")
        change = _ask_confirm("Change the pool file?", default=False)
        if change:
            config.pool_file = None

    if config.pool_file is None:
        use_pool = _ask_confirm("Do you have a response CSV to use?", default=False)
        if use_pool:
            pool_path = _ask_text("Path to CSV file")
            if Path(pool_path).exists():
                config.pool_file = pool_path
                _success(f"Pool set: {pool_path}")
            else:
                _warn("File not found — proceeding with fully synthetic responses.")
        else:
            _info("Using fully synthetic responses (randomly chosen from form options).")

    # ── Step 4: Run settings ─────────────────────────────────────────────────
    _section("Step 4 — Run Settings")

    config.count = _ask_number(
        "How many responses to submit?", default=1, min_val=1, max_val=config.max_runs
    )

    browser_choice = _ask_select(
        "Which browser?",
        choices=[
            _choice("auto", "Auto-detect my default browser (recommended)"),
            _choice("chrome", "Google Chrome"),
            _choice("firefox", "Mozilla Firefox"),
            _choice("edge", "Microsoft Edge"),
            _choice("headless", "Headless Chrome (invisible — faster)"),
        ],
        default="auto",
    )
    config.browser = browser_choice

    if browser_choice != "headless":
        config.headless = _ask_confirm("Run in headless (invisible) mode?", default=False)

    # ── Step 5: Preview ──────────────────────────────────────────────────────
    _section("Step 5 — Preview")

    do_preview = _ask_confirm("Preview a sample response before submitting?", default=True)
    if do_preview:
        _show_preview(config)

    # ── Step 6: Confirm & Run ────────────────────────────────────────────────
    _section("Step 6 — Submit")

    dry_run = _ask_confirm(
        f"Submit {config.count} response(s) to the form? (No = dry-run only)",
        default=True,
    )
    if dry_run:
        dry_run = False
    else:
        dry_run = True
        _warn("Dry-run mode — no browser will open, no submissions will happen.")

    save_config(config, config_path)
    _info(f"Config saved to {config_path}")

    _run_fill(config, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def _run_fill(config: RunConfig, dry_run: bool = False):
    from gformbot.generator import answer_stream
    from gformbot.filler import submit_response
    from gformbot.logger import log_result

    gen = answer_stream(config)

    if dry_run:
        _section("Dry Run Preview")
        for i in range(config.count):
            answer = next(gen)
            _show_answer(answer, config, index=i + 1)
        _success("Dry run complete — nothing was submitted.")
        return

    _section(f"Submitting {config.count} Response(s)")

    if _RICH:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Submitting…", total=config.count)

            for i in range(1, config.count + 1):
                answer = next(gen)
                progress.update(task, description=f"[{i}/{config.count}] Submitting…")
                try:
                    ok, message = submit_response(config, answer)
                    log_result(config.log_file, i, answer, ok, message)
                    progress.advance(task)
                    _success(f"[{i}/{config.count}] submitted")
                except KeyboardInterrupt:
                    _warn("\nStopped by user.")
                    break
                except Exception as e:
                    msg = str(e)
                    log_result(config.log_file, i, answer, False, msg)
                    _error(f"[{i}/{config.count}] {msg}")
                    _warn("Stopping to prevent repeated failures.")
                    break
    else:
        for i in range(1, config.count + 1):
            answer = next(gen)
            print(f"  [{i}/{config.count}] launching…")
            try:
                ok, message = submit_response(config, answer)
                log_result(config.log_file, i, answer, ok, message)
                print(f"  [{i}/{config.count}] submitted ✔")
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                msg = str(e)
                log_result(config.log_file, i, answer, False, msg)
                print(f"  [{i}/{config.count}] ERROR: {msg}")
                print("  Stopping to prevent repeated failures.")
                break

    _success(f"Log → {Path(config.log_file).resolve()}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_config(url, config_path, pool, browser, headless, count) -> RunConfig:
    """Load or generate a RunConfig from CLI flags."""
    if config_path:
        config = load_config(Path(config_path))
    elif Path(DEFAULT_CONFIG_NAME).exists():
        config = load_config(Path(DEFAULT_CONFIG_NAME))
    elif url:
        _info("No config found — inspecting form…")
        config = _run_inspect(url, browser=browser or "headless",
                              save=True, output=DEFAULT_CONFIG_NAME)
    else:
        _error("Provide a URL or a --config file.")
        raise SystemExit(1)

    if url:
        config.url = url
    if pool:
        config.pool_file = pool
    if browser:
        config.browser = browser
    if headless is not None:
        config.headless = headless
    if count is not None:
        config.count = count

    return config


def _show_preview(config: RunConfig):
    from gformbot.generator import answer_stream
    answer = next(answer_stream(config))
    _show_answer(answer, config)


def _show_answer(answer: dict, config: RunConfig, index: int = 1):
    if _RICH:
        t = Table(title=f"Sample Response #{index}", show_header=True,
                  header_style=f"bold {ACCENT}")
        t.add_column("Q#", style="dim", width=4)
        t.add_column("Question", min_width=35)
        t.add_column("Answer")
        for q in sorted(config.questions, key=lambda x: x.index):
            val = answer.get(q.index, "")
            if isinstance(val, list):
                val = ", ".join(val)
            t.add_row(str(q.index), q.text[:50], str(val)[:80])
        console.print(t)
    else:
        print(f"\n  Sample Response #{index}")
        for q in sorted(config.questions, key=lambda x: x.index):
            val = answer.get(q.index, "")
            if isinstance(val, list):
                val = ", ".join(val)
            print(f"  Q{q.index}: {val}")
        print()


def _open_editor(path: Path):
    editor = os.environ.get("EDITOR", "")
    if not editor:
        if platform.system() == "Windows":
            os.startfile(str(path))
            return
        editor = "nano" if shutil.which("nano") else "vi"
    subprocess.run([editor, str(path)])


def _choice(value, name: str):
    if _INQUIRER:
        from InquirerPy.base.control import Choice
        return Choice(value=value, name=name)
    return value  # fallback: just return the value string


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        cli(standalone_mode=False)
    except click.exceptions.Abort:
        print("\n  Aborted.")
    except SystemExit as e:
        raise
    except Exception as e:
        _error(f"Unexpected error: {e}")
        if os.environ.get("GFORMBOT_DEBUG"):
            raise
        raise SystemExit(1)


if __name__ == "__main__":
    main()
