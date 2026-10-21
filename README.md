# gformbot

Prepare synthetic Google Forms responses, review the exact answers, and fill the form from your terminal.

## Install

```bash
pip install gformbot
```

For development, run `python -m pip install -e ".[dev]"` from this repository. Use `python -m gformbot` if the `gformbot` command is not on your PATH.

Install Chrome, Firefox, or Edge for ChatGPT generation and form execution. Selenium manages the browser driver. Form inspection and CSV import do not need a browser.

Requires Python 3.10 or newer. The interactive file picker needs a graphical desktop and Python's Tk support (`python3-tk` on Linux).

## ChatGPT generation

Generation opens [ChatGPT](https://chatgpt.com/) in a fresh, visible private browser, sends the prepared prompt, and reads its JSON response automatically. The form, your instructions, reference frequencies, and up to six complete reference examples are sent to ChatGPT. The program validates every profile before producing CSV or filling the form.

No AI model download, local AI server, or API key is required. Website access and usage limits apply; generation may require signing in or completing a verification step yourself. The interactive wizard pauses when ChatGPT needs this attention. Website failures stop generation rather than silently substituting random answers. CSV import and random choice generation work without ChatGPT.

## Interactive workflow

```bash
gformbot
```

The wizard reads the form and displays its complete contents in a table: sections, questions, types, required fields, options, and supported constraints. Choose one of four sources, then a response count when needed:

| Source | What it does |
| --- | --- |
| **Generate responses (recommended)** | Generates complete profiles through ChatGPT. You can supply a reference CSV and instructions about the population, answer relationships, frequencies, or writing style. |
| **Use own data** | Import complete CSV rows, or choose a saved preview from a newest-first list without opening a file picker. Previews reuse their exact answers and response count. |
| **Enlarge existing responses** | Generates the requested number of additional profiles using an existing CSV as a reference. The original file stays intact. |
| **Random** | Randomizes multiple-choice, checkbox, dropdown, and supported numeric-scale answers. With reference data, automatic mode samples whole choice profiles to preserve relationships and selection counts. Manual weights override this. Enable automatic generation for written answers. |

Whenever a CSV is needed, the wizard waits for you to press Enter before opening the native file picker. Cancelling returns to the previous choice. It does not ask you to type a file path.

AI produces structured JSON internally. The program validates the answers and writes the CSV itself. Preparation also saves `gformbot-responses.json`, containing the exact batch shown in the preview.

Existing output files are preserved; the wizard adds a numbered suffix when needed. Generation speed depends on ChatGPT and the size of the batch.

After review, choose:

- **Save preview and exit** — save the prepared responses for a later run without opening a browser.
- **Fill for review** — the recommended Chrome background mode fills invisibly and opens a visible browser only when you choose **Open a response**. Prepare one at a time and submit before the next, or prepare the whole batch and choose which response to open. Reopening an already open response keeps your edits. For batches, choose **Check confirmations** after manually submitting; one-at-a-time review waits for confirmation automatically. **Exit and keep unsubmitted responses saved** preserves the batch. The tool never clicks Submit in this mode. Always-visible browsers also support one private tab per response. Headless and noninteractive runs only check filling and then close; they do not wait for manual submission.
- **Fill and submit** — explicitly confirm submission, then execute the prepared answers.

Results are logged with the complete prepared answers in `gformbot-runs.csv`. Manual edits in a review browser may differ from those prepared values; the preview and log retain the original prepared answers. A failed response stops the batch. Unretained choice selections get up to three attempts, with checkbox state checked before every retry.

After a completed run, the wizard's temporary JSON and generated CSV files are removed. Unconfirmed, preview-only, failed, and cancelled batches stay available. Confirmed submissions are saved in the plan as progress; reopening an interrupted batch skips those responses to avoid duplicates. Closing a tab before confirmation leaves that response unconfirmed. Imported/reference CSVs, edited output files, and the run log are preserved. Use `--keep-files` with `fill` or `run` to retain a completed batch. Explicit exports from the `generate --output` command stay intact.

To reopen a saved preview, start `gformbot`, open the same form, and choose **Use own data > Saved preview**. The list includes previews in the current folder and remembered locations, newest first; missing, invalid, and other-form plans are skipped. The small local index remembers file locations, while responses remain in their saved files. The CLI prints the exact replay command after saving as well.

Saved-preview replay offers review or submission directly; it does not ask you to save the same preview again. Background preparation reuses one invisible browser tab rather than opening a large batch of windows. Choosing Open fills the selected exact response in a visible private browser so you can edit or submit it.

## Commands

Inspect a public respondent link:

```bash
gformbot inspect "https://docs.google.com/forms/d/e/FORM_ID/viewform"
```

Short links such as `https://forms.gle/SHORT_ID` work in the wizard and all URL commands. Inspection resolves them to the full respondent URL automatically.

Generate responses with optional reference data and instructions:

```bash
gformbot generate "FORM_URL" --reference "responses.csv" --instructions "Generate varied student profiles" -n 10 -o "generated.csv"
```

Prepare and preview imported data without opening a browser:

```bash
gformbot fill "FORM_URL" --pool "responses.csv" --count 5 --dry-run
```

Fill without submitting, or explicitly submit:

```bash
gformbot fill "FORM_URL" --pool "responses.csv" --count 5 --browser chrome
gformbot fill "FORM_URL" --pool "responses.csv" --count 5 --submit
```

Use `--yes` with `--submit` to skip the confirmation in an unattended run.

Replay an exact saved batch:

```bash
gformbot run "gformbot-responses.json"
gformbot run "gformbot-responses.json" --submit
gformbot run "gformbot-responses.json" --browser chrome --keep-files
```

With no execution flags, an interactive `run` offers review or submission. `--browser chrome-background` fills invisibly and offers Open actions when running interactively. `--browser chrome` starts an always-visible rehearsal directly; add `--submit` to record the responses. `--headless` selects an invisible browser explicitly, without interactive Open actions.

Other commands:

```bash
gformbot preview --help
gformbot config --show
gformbot config --edit
gformbot log --last 20
gformbot --help
```

## CSV format

Use exact question titles, `Q1` / `Q2` style headers, or `entry.ID` identifiers. Unrelated columns such as a Google Forms timestamp can be ignored. Ambiguous mappings must be resolved before execution.

Single-choice cells contain one exact option label. Checkbox cells may contain JSON arrays, which preserve commas inside labels:

```csv
Q1,Q2
Student,"[""Roads, footpaths or crossings"", ""Bus stops""]"
```

Blank optional answers remain blank. Required answers and question constraints are checked before the browser opens. Generated responses are synthetic data; reference data and instructions guide their content but do not establish real-world accuracy.

Email-format validation is supported. Prepared date cells use `YYYY-MM-DD`, or `YYYY-MM-DD HH:MM` when the question includes time (24-hour clock). Questions without a year use `MM-DD` or `MM-DD HH:MM`. The inspection table shows the required format; ChatGPT receives it in the prompt. Invalid email addresses and impossible dates are rejected before execution.

Google Forms CSV dates such as `10/15/2023 14:30:00` are normalized automatically during import. The program infers month/day or day/month order from the entire column, preserves optional blanks, and leaves the original CSV unchanged. Ambiguous dates prompt for an order in the wizard; scripts can use `--date-order mdy` or `--date-order dmy` with `fill` or `generate`. Nonzero seconds are rejected because the form cannot represent them.

Forms requiring sign-in, file uploads, or unsupported question types or validation rules produce an actionable error. Use `inspect` to check a form before preparing a batch.

Browser navigation currently requires English Next/Submit labels. The browser session requests English, but forms with translated navigation may need their language changed. A submission with uncertain confirmation is never retried automatically.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m build
python -m twine check dist/*
```

Set `GFORMBOT_BROWSER_TESTS=1` to include Chrome integration checks against the local test form. These checks do not submit responses to Google.

## License

[Apache 2.0](LICENSE).
