# Changelog

## 0.3.0

### Workflow and recovery

- Resume saved batches and generation checkpoints from startup without rereading the form. Browse batch states with `batches` and check setup with `doctor`.
- Quiet review opens and fills only the response selected by the user. A separate headless fill test checks browser behavior while retaining the batch.
- Record execution intent and confirmation in a durable local journal before updating logs. Preserve original response numbers, skip confirmed rows, and require a human decision for uncertain outcomes.
- Checkpoint validated generation results and support resuming or exporting the accepted subset after interruption.

### Data and form handling

- Preserve whole imported profiles with explicit ordered, shuffled, and sampling-with-repetition choices. Keep `--ordered` compatibility.
- Show CSV mappings and require acknowledgment for unmatched optional questions. Reject malformed configurations, invalid fixed values, and multiline short-text answers before browser execution.
- Preserve instruction blocks and section descriptions in inspection and AI context without changing version-1 execution fingerprints.
- Parse long checkbox responses without recursion and index header mappings and option weights to avoid repeated scans. Make all legal random checkbox selection counts reachable.
- Improve date normalization, literal text entry, optional empty controls, choice-state verification, and browser cleanup.

### Packaging and compatibility

- Keep existing CLI commands and version-1 saved plans. Accept legacy page-delay settings without emitting inactive configuration knobs.
- Add distribution checks, installed-wheel smoke tests, a Python/OS test matrix, and local Chrome fixture CI. No package publishing or release creation is automated.

Supported form types and remaining browser/validation limits are documented in the README. Generated output still requires user review for meaning and statistical suitability.
