# gformbot

Generate synthetic Google Forms responses, reuse complete answer profiles, and submit forms from your terminal.

Supports public forms that accept anonymous responses. Google-account sign-in and file-upload questions are not supported.

## Install

```powershell
python -m pip install --upgrade gformbot
python -m gformbot
```

On macOS or Linux, use `python3` in both commands if that is your Python command. On Windows, use `py` in both if you use the Python launcher. Keeping the same interpreter for install and launch works even when the standalone `gformbot` command is missing from PATH.

Version `0.4.0` requires Python 3.10 or newer. One platform-independent wheel serves Windows, macOS, and Linux. Form inspection, submission, and AI generation run over HTTPS. Check your installed version with `python -m gformbot --version`.

To install the current source from GitHub, use `python -m pip install "git+https://github.com/yogeshkrishna/google_form_test_bot.git"` with Git installed.

The wizard's file picker opens after you press Enter. It needs a graphical desktop and Python's Tk support (`python3-tk` on Linux). Commands accept paths directly.

## Choose a workflow

**`generate` submits automatically over HTTPS.** It validates the exact prepared answers and selected route, sends only reached answers, and requires a Google confirmation before recording success. Use `prepare` to save answers for review or choose an action in the wizard.

| Command | Behavior |
| --- | --- |
| `gformbot` | Guided setup, answer sources, saved work, review, and execution choices. |
| `gformbot generate URL` | Prepare answers and submit directly over HTTP. No form browser or manual Submit step. |
| `gformbot prepare URL` | Prepare and save response JSON. AI preparation uses ChatGPT over HTTPS. |
| `gformbot preview [FILE.json]` | Open a JSON file in your system's default app. Without a file, choose saved response plans, newest first. This command does not fill or submit. |

Public respondent URLs and `forms.gle` short links work. In the wizard, **View this form's questions** displays questions, choices, sections and rules, then returns to the answer choices.

The start menu opens with **Quick submit**, which asks for the link, count, optional instructions and an optional guide CSV, then generates and submits automatically. **Start a new form** offers more answer sources and a review step. Press Backspace twice to return one step in an interactive terminal. In a text field, both presses must start with the field empty. **Continue response batch** appears when an interrupted batch remains. **More options** contains form inspection and setup checks.

Account-restricted forms, verified-account email collection and one-response-per-account forms stop before preparation or submission. Complete those forms manually or ask their owner for an anonymous test copy. Ordinary typed-email questions remain supported.

```bash
# Prepare answers for later.
gformbot prepare "FORM_URL" -n 10 -m "Varied fictional student profiles"

# View saved JSON in your default app.
gformbot preview
gformbot preview "gformbot-responses.json"

# Submit supplied rows automatically without opening a form browser.
gformbot generate "FORM_URL" --data "responses.csv" -n 5

# Use saved JSON, including resumable saved work.
gformbot generate "FORM_URL" --data "gformbot-responses.json"

# Generate from reference patterns, then submit.
gformbot generate "FORM_URL" -n 10 --like "reference.csv" -m "Include varied age groups"
```

Both `generate` and `prepare` accept `-n` for the response count, `-m` / `--message` for instructions, `--like` for a guide CSV, and `--data` for supplied CSV or saved JSON. Without `-n`, the commands use a matching saved form configuration's count or one response when none matches. Saved JSON carries its own response data and progress.

Use `--data` to execute supplied answers and `--like` to guide newly generated answers. Reference patterns and instructions guide generation; they do not establish statistical fidelity or semantic consistency.

## Guided mode

The wizard offers AI generation, your own data, or random choice answers. AI generation can create fresh fictional profiles or use an existing CSV as a guide. Both create new responses; a reference CSV is never appended to or overwritten. CSV import keeps complete rows together, with ordered, shuffled, or sampling-with-repetition choices.

Random mode supports multiple choice, checkboxes, dropdowns, and numeric scales. Automatic reference sampling keeps whole choice profiles together. Manual option weights are relative chances, not exact quotas. AI can complete written/date answers; optional unanswered fields can stay blank.

After preparation, one compact sample response appears. Choose **Submit N responses**, **Review or edit responses**, **Export remaining responses as CSV**, or **Back to start**. N is the number not confirmed submitted. Choosing Submit is the submission instruction; there is no second confirmation or browser chooser. Answers are already saved and validation runs automatically before execution.

Review or edit opens a small local editor in your default browser. It offers response navigation, search, choice controls and validation beside invalid answers. Saved edits keep the same batch identity and response numbers. Confirmed or uncertain responses are read-only, and a stale execution cannot submit edited answers. Choose Done to return to the CLI, or press Ctrl+C in the terminal if the tab was closed.

## Saved work and recovery

Response JSON stores prepared answers and execution progress. Continue response batch lists interrupted submissions; saved answers that have not started remain available through Use my own answers. Interrupted AI generation prints a separate draft recovery command. Supplied JSON can be used through `--data`. `preview` opens these files for viewing; use the wizard or `generate --data` to continue work.

A local submission journal records intent before a submission request and confirms it only after Google's completion page. Confirmed rows are skipped when work resumes. A request timeout or unrecognized outcome is uncertain and requires checking the form's responses before deciding whether to retry. Copied plans share their identity on the same machine. Duplicate protection depends on retaining the local journal.

Imported CSV/JSON files are never deleted. Files saved by `prepare` remain available. Temporary response JSON owned by the tool is removed only after every response is confirmed in the submission journal; unfinished or uncertain work stays saved. Older files with missing retention settings or ambiguous ownership (including legacy command output) stay saved. Using a saved file as `--data` preserves that input.

Run logs record submitted answers and outcomes.

Form directions and link destinations are saved from the introduction, visited sections, question descriptions, and the confirmed completion screen. A short **Next steps** summary shows links and custom completion messages once. Read everything later with `gformbot log --followups`, or `gformbot log "custom-runs.csv" --followups` for a custom log. `--last` limits the number of responses shown. These notes stay in the local journal after temporary response files are cleaned up.

Completion messages are captured only after an HTTP confirmation. Internal request validation and manually reconciling an uncertain result do not create a completion observation. The tool never follows these links or performs outside actions. Public group invites are preserved; embedded URL credentials, account/edit-response links and known secret URL parameters are excluded or redacted. Redacted links may not work. Ordinary instruction text is retained without automatic secret detection.

## AI generation

AI generation uses ChatGPT's undocumented Android guest service over HTTPS with a temporary session. The service can change, deny guest access, or impose usage limits. GFormBot reports access failures and retains accepted work in the generation draft. Resume when the service is available, or use `--data` with a CSV or a prepared response plan.

GFormBot sends a form-specific writing brief, original question text, options and rules, and reference choice frequencies with up to six examples. It tracks repeated patterns across the batch. Later groups carry relevant earlier answers and protected facts. Linked or evidence-dependent answers can receive a focused review and repair before the plan is saved. Ordinary form validation and model review cannot establish factual accuracy or representativeness.

Generation uses adaptive batches. Estimated answer sizes determine the initial count, then observed reply sizes adjust later batches. Each HTTPS request carries its own context. Large prompts use a more compact representation. GFormBot validates complete JSON records individually and requests bounded corrections for missing or invalid profiles.

When a validation error can be isolated, the correction names the affected answer keys and rules. The model returns just those answers, and GFormBot validates the combined result before accepting the profile.

Large forms are generated in smaller question groups. Routing answers select a supported terminating path first. Grid rows stay together. Profiles regroup where possible after branches rejoin. Later groups receive relevant earlier answers and recent context; complete answers remain stored locally. Repeated context is shared, with at most two generation requests running together. A response is accepted only after the assembled profile passes the original form's validation. If ChatGPT signals a temporary slowdown before returning answers, both workers pause and generation continues one request at a time in the same session. The tool honors the server's wait time within a 60-second recovery budget. An exhausted allowance, an unexplained limit, or a longer requested wait stops generation and reports the available details.

AI generation saves each accepted reply locally before reporting saved progress. Simple forms save complete profiles, and partitioned forms save completed question groups. After an interruption, use the printed `gformbot prepare "FORM_URL" --resume "DRAFT_PATH"` command to continue missing work with the original response identities. Compatible drafts from the former browser mode resume over HTTPS, using question groups where needed. Drafts contain plaintext form context, instructions, reference data and answers. Changed forms and invalid drafts are rejected. Submission progress is tracked separately.

Generated contact fields use realistic formats and distinct values within each batch. Corrections keep the respondent's saved name and location in context. For exact test contacts, use an explicit quality policy or supply your own data. The reviewer checks relevant free-text answers against choices, dates and quantities on short forms as well as branching forms.

`prepare` and `generate` accept `--quality POLICY.json` for exact requirements and extra context. This example derives the email in Q7 from the name in Q6, checks the phone in Q8, and reviews the answer in Q13 against Q1 and Q5. Use your own question numbers and test domain.

```json
{
  "style": "Use natural order details and occasional short pickup notes.",
  "facts": [1, 5, 6],
  "dependencies": {"Q13": [1, 5]},
  "fields": {
    "Q7": {"email_domain": "bakery-test.example", "name_from": 6},
    "Q8": {"min_digits": 10}
  }
}
```

Field policies also accept `exact`, an `approved` value list, or `route_values` keyed by a comma-separated route such as `"1,3,4"`. Use `coverage` for explicit coverage of eligible choice fields, or a guide CSV for existing response patterns. Supply observed facts with `"evidence": {"Q4": "The image shows a square."}`. For transport tests, `allow_placeholders` lists fields the user allows to use placeholder answers. Add evidence or placeholders to a saved draft by combining `--resume` with a policy file containing those keys.

Outgoing requests redact known secret URL parameters and omit embedded credentials, account links and edit-response links. Public invitation links remain available. Review ordinary instruction text for private details before sending it. Keep saved form metadata and answers in a private directory. Draft recovery uses files created by the current generation workflow.

## Data and form support

CSV columns map through question titles, `Q1` / `Q2` headers, or `entry.ID` identifiers. Review ambiguous or unmatched mappings in the wizard. Blank optional answers stay blank; missing required answers must be resolved. Reference CSVs also need complete valid profiles.

Single-choice values use exact option labels. Checkbox JSON arrays preserve commas inside labels:

```csv
Q1,Q2
Student,"[""Roads, footpaths or crossings"", ""Bus stops""]"
```

Short text occupies one line; paragraphs may contain newlines. Number comparisons/ranges, whole numbers, text contains/does-not-contain, character limits, email and checkbox constraints share the same checks across CSV import, AI output, editing, and execution. Number answers retain their original text. Character limits count UTF-16 units as Google's client does, so an emoji can count as two characters. Checkbox repairs are explicit and recorded.

Pattern validation supports verified JavaScript-compatible syntax and all four Forms pattern operations. Checks run in one lazy background process with bounded waits and caches; no regex dependency is installed. Unverified syntax (including backreferences, named groups, lookbehind, inline flags, and `\S` inside character classes) stops explicitly. Patterns are limited to 4,096 characters including operation wrappers, and answers to one million UTF-16 units for pattern checks. URL-format rules are checked locally; Google's own validation remains authoritative. Unknown validation families stop explicitly. A valid filled page is not proof of submission.

Dates use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`; yearless questions use `MM-DD` with optional time. Google Forms CSV dates are normalized without changing the source. For ambiguous dates, `generate` and `prepare` accept `--date-order mdy` or `--date-order dmy`; the wizard asks you to choose. An explicit option overrides matching saved settings. Impossible dates and nonzero seconds are rejected.

Clock time uses `HH:MM` (00:00–23:59); duration uses `HH:MM:SS` (hours 00–72, minutes/seconds 00–59). CSV clock values may include AM/PM or zero seconds; nonzero clock seconds are rejected rather than discarded. No timezone conversion is applied.

Icon ratings (stars, hearts and thumbs) retain their question type and use exact numeric strings such as `"4"`; required, optional, import/export and choice-weight behavior matches other single-choice questions.

For an actual Other selection, use `{"choices": ["Python"], "other": "Rust"}` (radio: empty `choices`). Other counts as one checkbox selection. CSV cells use the same JSON object; unknown labels are never silently interpreted as Other. Ordinary strings/lists remain unchanged. Plans requiring Other use format 2; older plans retain their identities and progress.

Form inspection lists images and videos in form order for your reference. To use something you saw in the original form, type the relevant facts in **Extra instructions** or pass them with `-m`.

Form submission does not interact with browser controls. HTTP execution validates routing and inspects the server response; unknown or conflicting server navigation metadata stops explicitly.

File-upload questions are unsupported and never transfer files. Older plans containing prepared uploads cannot execute; their files and journal records are preserved. CSV/JSON import and export still work normally.

Supported questions include short text, paragraphs, multiple choice, checkboxes, dropdowns, numeric scales, icon ratings, explicit Other answers, dates with optional year/time, clock time, duration, and both grid types. Grid rows use their entry IDs or Google CSV headers (`Question [Row]`); required rows and one-response-per-column rules are checked together. Section routing follows each response's choices, including skipped sections and early Submit. Only reached questions require answers; repeated sections stop with an error. Instruction blocks and section descriptions are retained as text. Unsupported controls and validation block their sections. A response can avoid those sections only when its complete route is understood; unclear routing and legacy unscoped compatibility findings still block the form. Generated routes respect fixed choices and positive weights while avoiding unsupported sections and loops. Closed or restricted forms retain unfinished work.

## Other commands

```bash
gformbot config --show
gformbot config --edit
gformbot log --last 20
gformbot log --followups
gformbot doctor
gformbot doctor --check
gformbot --help
gformbot --version
```

`doctor --check` exits unsuccessfully when a required local prerequisite is missing. Runtime errors go to stderr. Set `GFORMBOT_DEBUG=1` to retain a traceback when diagnosing a failure; avoid sharing logs containing private responses.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m build
python -m twine check dist/*
```

CI runs offline tests on Windows, macOS, and Linux. It builds the wheel and source archive and smoke-tests installed commands outside the checkout. HTTP fixtures cover generation, routing, submission confirmation, and recovery without live requests.

## License

[Apache 2.0](LICENSE). The ChatGPT protocol adaptation includes a separate [MIT notice](LICENSE-chatgpt-protocol).
