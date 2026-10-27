# Changelog

## Unreleased

- Support both grid types across CSV, saved answers, generation, and verified filling, including shuffled rows and column restrictions.

- Validate number comparisons, text rules, character limits and supported regex patterns through one shared path for CSV, AI output, editing and browser execution. Preserve imported values, display readable constraints, and isolate regex checks with bounded waits without adding dependencies.

- Decode compact Google date settings and exact checkbox counts correctly. Add `--date-order` to `generate`/`prepare`, retain prepared and ambiguous legacy files consistently, and require journal confirmations before temporary-file cleanup.

- Bundle a private Windows x64 Selenium runtime with only supported browser adapters, their synchronous dependencies and the unchanged Windows Manager. Remove unused OS binaries, generated protocols and asynchronous dependencies; retain official Selenium on other platforms.
- Keep directions and links from visited form pages and confirmed completion screens. Show a short next-steps summary and add `log --followups` for saved instructions across automatic and manual review modes, without affecting submission recovery.
- Follow answer-based section routes across CSV import, generation, previews, and filling, including skipped required questions and early Submit. Stop before submission if the browser deviates from the planned route.
- Add explicit Google sign-in with a shared temporary browser session, email collection support, and checks for known response limits.
- Report blocked forms clearly and keep manual or automatic submission outcomes conservative when access changes.

## 0.3.1

- Reduce the public CLI to the guided wizard, `generate`, `prepare`, and file-based `preview`, alongside configuration, logs, diagnostics, help, and version.
- Make explicit `generate` execution fill and submit by default. Add `--halt` for filled tabs that stay open for manual submission. The wizard continues to ask which action to take.
- Add shared count, instruction, reference-data, input-data, and browser options. Accept CSV and saved response JSON through `--data`.
- Save persistent response JSON with `prepare`. Make `preview` open JSON in the default app, with a newest-first chooser for unfinished responses and AI progress.
- Keep generation recovery in the guided menu. Preserve imported CSV/JSON and prepared files; clean up tool-owned temporary response JSON only after confirmed completion.
- Retire the separate inspection, filling, replay, batch-listing, and generation-recovery command routes in favor of these workflows.

## 0.3.0

- Add offline saved-work recovery, generation checkpoints, quiet review, headless fill checks, and a durable submission journal with original response IDs and uncertain-outcome reconciliation.
- Preserve whole CSV profiles with explicit sampling modes, review column mappings, validate configuration values, and retain form instructions and section descriptions.
- Improve long checkbox parsing, header lookup, serialization, literal text filling, date handling, choice verification, and browser cleanup.
- Add distribution checks, wheel-install smoke tests, a Python/OS test matrix, and local Chrome fixtures. Preserve version-1 saved plans and legacy configuration migration.

Generated output requires review for meaning and statistical suitability. Package publishing and release creation are not automated.
