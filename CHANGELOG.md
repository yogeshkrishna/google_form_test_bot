# Changelog

## 0.4.4

- Quick submit uses the same preparation and checks as the normal wizard, then sends the full batch automatically. Both wizards accept a pasted CSV path or the file picker.
- Show the current AI stage and elapsed time while waiting. Repair missing fields without regenerating independently valid answers or changing a verified route.
- Reject CSVs with no matching columns before showing a mapping table. Random answers without AI use routes that can be completed with the available choices.
- Use my own answers can copy a prompt for creating the form's CSV.
- Rename the recovery menu to Continue batches.
- Use question-number media observations across AI workflows, and keep CSV-guided random answers on the selected route.
- Load media links in the background and open them with a single click in supported terminals.
- Clear keypresses queued during work before showing the Start menu again.
- Continue reviewing independent responses if a review group fails. Keep saved batches resumable across interruptions.
- Require supporting quotes for AI review findings that compare answers. Restore duplicate-contact guidance when resuming a synthetic batch.
- Reduce local work when packing AI prompts, saving drafts, and recording form instructions before submission.
- Skip AI route selection for responses with a verified preset route, and size question groups against the actual prompt and estimated reply.
- Keep generating other responses when an individual response exhausts its retries. Continue the saved draft directly from the wizard or run `prepare --resume` with the draft file alone.
- Keep batches large when individual answers need correction. Generate and review responses across branches together when they fit within the prompt limits.
- Batch field corrections separately from new responses, track generated contact collisions, and grow review batches after complete replies.

## 0.4.3

- Show the form introduction before the question table and open the preview automatically in Start a new form.
- Show repeated confirmation messages once and highlight follow-up links after submission.
- Resume interrupted AI generation and review from Saved batches, with progress and timestamps.
- Edit multiple responses and save them together. Pick up links copied after the prompt opens and use a plain menu pointer.

## 0.4.2

- Lead the README and PyPI quickstart with Quick submit and synthetic response generation.

## 0.4.1

- Pause automatic submission when AI flags an answer and require confirmation after review.
- Open ready and interrupted responses from Saved batches. View recent results, full answers, and follow-up instructions under More options.
- Clarify email policy errors, validate preview files, and reject conflicting config flags.
- Use stable artwork URLs so the README images load on GitHub and PyPI.

## 0.4.0

- Generate and review answers with ChatGPT over HTTPS. Use adaptive batches and route-aware question groups, carry relevant earlier answers forward, and correct invalid fields or contradictory answers in place.
- Save validated work locally for `prepare --resume`. Preserve compatible older drafts and honor bounded waits for temporary throttling.
- Submit public anonymous forms over HTTPS. Verify the receipt, skip confirmed submissions, and ask before retrying an uncertain outcome.
- Add Quick submit, double-Backspace navigation, and empty count fields that accept their displayed default on Enter. Form questions can be opened on demand without leaving answer selection.
- Show one sample response before submitting from the guided workflow. Review or edit through a local browser editor with shared validation and protection for submitted and uncertain rows. Export remaining responses or resume an interrupted submission.
- Generate distinct contact details in realistic local or international formats. Redact known private links and keep media metadata in form inspection.
- Support branching and early submission, text and number rules, dates and times, both grid types, ratings, typed email and explicit Other answers. Reject unsupported questions on the chosen route, account restrictions and file uploads.
- Capture form directions and completion links, retain prepared files and legacy batch progress, and fix concurrent log and config writes. Add CSV date order and clearer diagnostics.
- Ship a universal Python wheel. Remove Selenium, browser drivers, and the `--browser` option. Test installed console and module launchers outside the checkout.

## 0.3.1

- Reduce the public CLI to the guided wizard, `generate`, `prepare`, and file-based `preview`, alongside configuration, logs, diagnostics, help, and version.
- Make explicit `generate` execution fill and submit by default. Add `--halt` for filled tabs that stay open for manual submission. The wizard continues to ask which action to take.
- Add shared count, instruction, reference-data, input-data, and browser options. Accept CSV and saved response JSON through `--data`.
- Save persistent response JSON with `prepare`. Make `preview` open JSON in the default app, with a newest-first chooser for unfinished responses and AI progress.
- Keep generation recovery in the guided menu. Preserve imported CSV/JSON and prepared files; clean up tool-owned temporary response JSON only after confirmed completion.
- Retire the separate inspection, filling, replay, batch-listing, and generation-recovery command routes in favor of these workflows.

## 0.3.0

- Add offline saved-work recovery, generation checkpoints, quiet review, request checks, and a durable submission journal with original response IDs and uncertain-outcome reconciliation.
- Preserve whole CSV profiles with explicit sampling modes, review column mappings, validate configuration values, and retain form instructions and section descriptions.
- Improve long checkbox parsing, header lookup, serialization, literal text filling, date handling, choice verification, and browser cleanup.
- Add distribution checks, wheel-install smoke tests, a Python/OS test matrix, and local Chrome fixtures. Preserve version-1 saved plans and legacy configuration migration.

Generated output requires review for meaning and statistical suitability. Package publishing and release creation are not automated.
