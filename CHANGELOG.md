# Changelog

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
