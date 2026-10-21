# gformbot

Prepare synthetic Google Forms responses, inspect the exact answers, and fill forms from your terminal. Import complete CSV profiles, generate profiles through ChatGPT, or randomize choice questions.

## Install

```bash
python -m pip install gformbot
gformbot
```

Requires Python 3.10 or newer. Use `python -m gformbot` if the command is not on your PATH. Install Chrome, Firefox, or Edge for browser execution; Selenium manages the driver. Inspection, saved-batch browsing, CSV import, and preview do not require a browser.

The interactive file picker needs a graphical desktop and Python's Tk support (`python3-tk` on Linux). It opens only after you press Enter. Scripted commands accept file paths directly.

## Interactive workflow

Start `gformbot` to prepare a new batch or resume saved work. Resume saved batches or generation checkpoints without reading the form again. Saved batches appear newest first.

For a new batch, paste a public respondent URL, including a `forms.gle` short link. The tool displays sections, instructions, descriptions, questions, options, required fields, and supported constraints in a table. Choose a response source:

| Source | Behavior |
| --- | --- |
| **Generate responses** | Choose fresh synthetic profiles, match a reference CSV's style/distribution, or generate additional profiles from existing responses. Supply additional instructions if needed. The original reference file stays intact. |
| **Use own data** | Import complete CSV rows or reopen an exact saved preview without a file picker. Choose original order, shuffle without repetition, or sampling with repetition. |
| **Random** | Randomize multiple choice, checkboxes, dropdowns, and numeric scales. Optionally use reference choice profiles or set relative option weights. ChatGPT can complete written/date answers; optional unanswered fields may stay blank. |

CSV import validates complete rows and shows column mappings. Unmatched optional questions require acknowledgment before leaving their answers blank. Ambiguous mappings and missing required answers must be resolved. Reference CSVs must also contain valid complete profiles.

Automatic random mode samples whole reference choice profiles when supplied. Manual weights apply to individual choices; they are relative probabilities, not exact quotas. Imported profiles always keep their answers together.

Prepared answers are saved before execution. Inspect individual profiles, edit a copy as a new batch, export CSV, or choose how to continue:

| Action | What happens |
| --- | --- |
| **Save preview** | Keep the exact batch for later without opening a browser. The CLI prints its replay command. Reopened batches go directly to their available actions. |
| **Review** | Quiet mode opens no browser until **Open a response**. Only that profile is filled in a visible private browser. Edit or submit it yourself; the tool never clicks Submit. Choose one-at-a-time review or separate response tabs. Already open responses retain your edits. |
| **Test fill** | Fill invisibly to check browser compatibility, then close. Submit nothing and retain the saved batch. |
| **Submit** | Confirm explicitly, then fill and submit the prepared answers. |

Review can wait for one manual submission before opening the next, or check confirmations across open tabs. Closing a tab without confirmed submission keeps its response pending. Leaving review preserves unfinished work.

## ChatGPT generation

Generation opens [ChatGPT](https://chatgpt.com/) in a fresh, visible private browser and reads structured JSON automatically. No model download, local AI server, or API key is required. Website limits apply; you may need to sign in or complete verification. Browser setup is checked before generation starts.

The form context, your instructions, reference frequencies, and up to six complete reference examples are sent to ChatGPT. Every returned profile is validated before export or execution. Validation checks supported formats, options, and constraints; it does not prove semantic consistency, statistical fidelity, or real-world accuracy.

Accepted profiles are checkpointed as generation proceeds. Interruptions preserve validated work for resuming or partial export. Website failures report an error rather than silently replacing answers with random data. CSV import and choice randomization work without ChatGPT.

## Commands

```bash
# Inspect a form and save its editable configuration.
gformbot inspect "FORM_URL"

# Generate profiles; reference data and instructions are optional.
gformbot generate "FORM_URL" -n 10 --reference "responses.csv" --instructions "Varied student profiles" --checkpoint "generation.json" -o "generated.csv"

# Resume generation, or export only profiles already validated.
gformbot generation "generation.json" --resume
gformbot generation "generation.json" --export "valid.csv"

# Prepare imported rows without a browser.
gformbot fill "FORM_URL" --pool "responses.csv" -n 5 --sampling ordered --dry-run

# Shuffle without repetition, or sample with repetition.
gformbot fill "FORM_URL" --pool "responses.csv" -n 5 --sampling shuffle --dry-run
gformbot fill "FORM_URL" --pool "responses.csv" -n 20 --sampling sample --dry-run

# Test filling invisibly, preserving the saved batch.
gformbot run "gformbot-responses.json" --headless

# Review; open a browser only when requested.
gformbot run "gformbot-responses.json" --browser auto-background

# Submit only after confirmation.
gformbot run "gformbot-responses.json" --submit

# Browse saved batch states, diagnose setup, or inspect logs.
gformbot batches
gformbot doctor
gformbot log --last 20
```

`--ordered` remains an alias for `--sampling ordered`. Without either flag, scripted CSV filling retains its existing sampling-with-repetition behavior. Ordered and shuffled batches cannot exceed the CSV row count. Saved batches always reuse their exact prepared rows and count.

`generate` and `fill` accept `--date-order mdy` or `--date-order dmy` for ambiguous CSV dates. `fill` and `run` accept `--submit --yes` for explicitly authorized unattended submission and `--keep-files` to retain completed artifacts. `--dry-run` prepares data without exercising the browser; `--headless` actually tests filling.

The existing `preview`, `config --show`, and `config --edit` commands remain available. Run `gformbot COMMAND --help` for all options. `generation CHECKPOINT` without an action offers resume or partial export interactively.

## Saved work and recovery

New batches and generation checkpoints use separate files. The wizard preserves existing outputs with numbered filenames. Select saved previews at startup, through **Use own data**, or with `run`. The local index remembers locations; responses remain in their files.

A durable local journal records attempted, confirmed, and uncertain submissions independently of the display log. Resuming skips confirmed rows using their original row numbers. Uncertain confirmation stops execution and requires you to check the form's responses before deciding whether to retry. Copied plans share their batch identity on the same machine. Duplicate prevention depends on retaining the local journal; moving or deleting it can remove that protection.

Run logs contain prepared answers and outcomes. Manual browser edits can differ from those answers. Reopening a still-open tab retains edits; a restarted session fills the saved prepared answers.

Completed wizard-owned files can be cleaned up after every response is confirmed. Unfinished, failed, preview-only, and test-fill batches stay available. Imported/reference CSVs and explicit exports are preserved. Version-1 saved plans remain readable. Legacy page-delay settings are accepted for compatibility and omitted from newly saved configurations.

## CSV format

Map columns using exact question titles, `Q1` / `Q2` headers, or `entry.ID` identifiers. Unrelated columns such as timestamps may be ignored after reviewing the mapping. Blank optional answers remain blank.

Single-choice cells contain exact option labels. Use JSON arrays for checkboxes when labels contain commas:

```csv
Q1,Q2
Student,"[""Roads, footpaths or crossings"", ""Bus stops""]"
```

Short text must occupy one line; paragraphs may contain newlines. Email-format validation is supported. Checkbox repairs are opt-in, limited to supported duplicate/exclusive/count problems, and recorded. Other invalid answers require correction.

Prepared dates use `YYYY-MM-DD`, or `YYYY-MM-DD HH:MM` when time is included. Yearless questions use `MM-DD` or `MM-DD HH:MM`. Time uses a 24-hour clock. Google Forms exports are normalized without modifying the source CSV. The importer infers month/day order across the column; ambiguous columns require a choice. Impossible dates and nonzero seconds are rejected.

## Supported forms

| Supported | Limits |
| --- | --- |
| Short text, paragraphs, multiple choice, checkboxes, dropdowns, numeric scales, dates with optional time/year | Inspection displays supported constraints, including email and checkbox count validation. |
| Multiple sections, titles, descriptions, instruction blocks | Text context is preserved for review and generation; images and video are not rendered. |
| Public respondent URLs and shortened links | Forms requiring sign-in or permission are rejected with an actionable error. |

Uploads, grids, standalone time/duration, unsupported rating types, branching, free-text “Other,” and unsupported custom validation stop execution. Browser navigation currently expects English Next/Submit labels. Inspect unfamiliar forms first.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m build
python -m twine check dist/*
```

Set `GFORMBOT_BROWSER_TESTS=1` to run Chrome integration fixtures. These use a local test server, including local submission handling; they do not submit to Google or call live AI. Current browser integration coverage exercises Chrome. Firefox and Edge share the browser adapter and need their own environment checks.

CI runs unit checks across Windows and Ubuntu on Python 3.10, 3.12, 3.13, and 3.14, checks distributions, smoke-tests an installed wheel outside the source directory, and runs a separate Ubuntu Chrome fixture job. It uploads build artifacts without publishing packages or creating releases.

## License

[Apache 2.0](LICENSE).
