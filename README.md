# gformbot

Generate synthetic Google Forms responses, reuse complete answer profiles, and fill forms from your terminal.

## Install

```bash
python -m pip install gformbot
gformbot
```

Requires Python 3.10 or newer and an installed Chrome, Firefox, or Edge browser for generation and filling. Selenium manages the driver. Use `python -m gformbot` if the command is not on your PATH.

Windows x64 wheels include a private Selenium subset with the original Windows driver manager. Other platforms use official Selenium. Existing Selenium installations can coexist; pip does not remove old shared dependencies during an upgrade. [Build details and measurements](docs/windows-selenium.md).

The wizard's file picker opens after you press Enter. It needs a graphical desktop and Python's Tk support (`python3-tk` on Linux). Commands accept paths directly.

## Choose a workflow

**`generate` fills and submits automatically.** Use `prepare` to save answers without filling a form, or add `--halt` to keep filled tabs open and click Submit yourself. The no-argument wizard asks which action to take.

| Command | Behavior |
| --- | --- |
| `gformbot` | Guided setup, answer sources, saved work, review, and execution choices. |
| `gformbot generate URL` | Prepare answers, fill the form, and submit. `--halt` stops before submission and keeps the filled tabs open. |
| `gformbot prepare URL` | Prepare and save response JSON without filling or submitting a form. AI preparation can still open ChatGPT. |
| `gformbot preview [FILE.json]` | Open a JSON file in your system's default app. Without a file, choose unfinished saved responses or saved AI work, newest first. This command does not fill or submit. |

Public respondent URLs and `forms.gle` short links work. The wizard displays sections, instructions, descriptions, questions, choices, required fields, and supported constraints before preparing answers.

For a form that requires Google login, use `gformbot generate "FORM_URL" --sign-in` (or `prepare --sign-in`). Sign in directly in the temporary form browser; the tool reuses that session and stores no credentials in response files. This requires an interactive terminal and a visible browser. Review and check verified-email consent yourself when prompted. Google may refuse automated-browser login; access and per-account response limits still apply.

```bash
# Prepare answers for later.
gformbot prepare "FORM_URL" -n 10 -m "Varied fictional student profiles"

# View saved JSON in your default app.
gformbot preview
gformbot preview "gformbot-responses.json"

# Reuse supplied rows and review filled tabs yourself.
gformbot generate "FORM_URL" --data "responses.csv" -n 5 --halt

# Use saved JSON, including resumable saved work.
gformbot generate "FORM_URL" --data "gformbot-responses.json" --halt

# Generate from reference patterns, then fill and submit.
gformbot generate "FORM_URL" -n 10 --like "reference.csv" -m "Include varied age groups"
```

Both `generate` and `prepare` accept `-n` for the response count, `-m` / `--message` for instructions, `--like` for a reference CSV, `--data` for supplied CSV or saved gformbot JSON, and `--browser` for browser selection. `--halt` belongs to `generate`. Without `-n`, the tool uses a matching saved form configuration's count, or one response when none matches. Saved JSON carries its own response data and progress. Output filenames are chosen automatically.

Use `--data` to execute supplied answers and `--like` to guide newly generated answers. Reference patterns and instructions guide generation; they do not establish statistical fidelity or semantic consistency.

## Guided mode

The wizard offers AI generation, your own data, or random choice answers. AI generation can create fresh fictional profiles, follow a reference CSV, or add profiles based on existing responses. CSV import keeps complete rows together, with ordered, shuffled, or sampling-with-repetition choices.

Random mode supports multiple choice, checkboxes, dropdowns, and numeric scales. Automatic reference sampling keeps whole choice profiles together. Manual option weights are relative chances, not exact quotas. AI can complete written/date answers; optional unanswered fields can stay blank.

Review the prepared answers, view a response, edit a copy, export CSV, save for later, test filling without submission, or explicitly choose submission. Quiet review opens no browser until you select a response. Already open responses keep your edits; restarting fills the saved prepared answers.

## Saved work and recovery

Response JSON stores prepared answers and execution progress. The wizard also lists saved AI progress and can resume accepted work after an interruption. Supplied JSON can be used through `--data`. `preview` opens these files for viewing; use the wizard or `generate --data` to continue work.

A local submission journal records attempts and confirmations. Confirmed rows are skipped when work resumes. An uncertain outcome requires checking the form's responses before deciding whether to retry. Copied plans share their identity on the same machine; duplicate protection depends on retaining the local journal.

Imported CSV/JSON files are never deleted. Files saved by `prepare` remain available. Temporary response JSON owned by the tool is removed only after every response is confirmed in the submission journal; unfinished or uncertain work stays saved. Older files with missing retention settings or ambiguous ownership (including legacy command output) stay saved. Using a saved file as `--data` preserves that input.

Run logs record prepared answers and outcomes. Manual changes in a browser can differ from the prepared values.

Form directions and link destinations are saved from the introduction, visited sections, question descriptions, and the confirmed completion screen. A short **Next steps** summary shows links and custom completion messages once. Read everything later with `gformbot log --followups`, or `gformbot log "custom-runs.csv" --followups` for a custom log. `--last` limits the number of responses shown. These notes stay in the local journal after temporary response files are cleaned up.

Completion messages are captured only when the browser confirms submission, including manual review and `--halt`. Filling alone or manually reconciling an uncertain result does not create a completion observation. The tool never follows these links or performs outside actions. Public group invites are preserved; embedded URL credentials, account/edit-response links and known secret URL parameters are excluded or redacted. Redacted links may not work. Ordinary instruction text is retained without automatic secret detection.

## ChatGPT generation

AI generation uses [ChatGPT](https://chatgpt.com/) in a visible private browser, with no model download, local server, or API key. Website limits apply; you may need to sign in or complete verification yourself. Browser setup is checked before generation.

The form context, instructions, reference frequencies, and up to six complete reference examples are sent to ChatGPT. Returned profiles are checked against supported formats, choices, and constraints. These checks do not prove that generated answers are meaningful, representative, or accurate. Failed generation preserves accepted work instead of substituting random answers.

## Data and form support

CSV columns map through question titles, `Q1` / `Q2` headers, or `entry.ID` identifiers. Review ambiguous or unmatched mappings in the wizard. Blank optional answers stay blank; missing required answers must be resolved. Reference CSVs also need complete valid profiles.

Single-choice values use exact option labels. Checkbox JSON arrays preserve commas inside labels:

```csv
Q1,Q2
Student,"[""Roads, footpaths or crossings"", ""Bus stops""]"
```

Short text occupies one line; paragraphs may contain newlines. Number comparisons/ranges, whole numbers, text contains/does-not-contain, character limits, email and checkbox constraints share the same checks across CSV import, AI output, editing, and execution. Number answers retain their original text. Character limits count UTF-16 units as Google's client does, so an emoji can count as two characters. Checkbox repairs are explicit and recorded.

Pattern validation supports verified JavaScript-compatible syntax and all four Forms pattern operations. Checks run in one lazy background process with bounded waits and caches; no regex dependency is installed. Unverified syntax (including backreferences, named groups, lookbehind, inline flags, and `\S` inside character classes) stops explicitly. Patterns are limited to 4,096 characters including operation wrappers, and answers to one million UTF-16 units for pattern checks. URL/phone rules and other unknown validation families remain unsupported. The browser's own validation and positive submission confirmation still apply.

Dates use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`; yearless questions use `MM-DD` with optional time. Google Forms CSV dates are normalized without changing the source. For ambiguous dates, `generate` and `prepare` accept `--date-order mdy` or `--date-order dmy`; the wizard asks you to choose. An explicit option overrides matching saved settings. Impossible dates and nonzero seconds are rejected.

Supported questions include short text, paragraphs, multiple choice, checkboxes, dropdowns, numeric scales, and dates with optional year/time. Section routing follows each response's choices, including skipped sections and early Submit. Only reached questions require answers; repeated sections stop with an error. Instruction blocks and section descriptions are retained as text. Uploads, grids, standalone time/duration, unsupported rating types, free-text “Other,” and unsupported custom validation stop execution. Browser navigation and verified-email consent expect English labels. Known one-response limits stop incompatible batches; closed, restricted, or already-answered forms retain unfinished work.

## Other commands

```bash
gformbot config --show
gformbot config --edit
gformbot log --last 20
gformbot log --followups
gformbot doctor
gformbot --help
gformbot --version
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m build
python -m twine check dist/*
```

Set `GFORMBOT_BROWSER_TESTS=1` for local Chrome fixtures. These exercise a local test server without Google submissions or live AI. Firefox and Edge need their own environment checks.
Set `GFORMBOT_TEST_BROWSER=edge` or `firefox` to run the same fixtures against that installed browser.

CI checks Windows/Ubuntu on Python 3.10, 3.12, 3.13, and 3.14, builds both wheel variants, smoke-tests them outside the checkout, and runs Chrome fixtures on Ubuntu plus Chrome/Edge/Firefox fixtures on Windows. It uploads build artifacts without publishing packages or creating releases.

## License

[Apache 2.0](LICENSE).
