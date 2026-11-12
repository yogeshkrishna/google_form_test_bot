# Working on GFormBot

Keep changes small enough to explain, test, and review.

## Before changing code

Check existing issues and `git status`. Describe the failure, expected behavior, and a small reproduction. Agree on the scope before adding a feature or changing the architecture. Preserve any existing working changes.

Use one short branch per fix, based on the current default branch (`master`). Preserve unfinished work when branching.

## Build the fix

1. Add a regression case that fails for the reported problem. Prefer sanitized form metadata and local HTML fixtures over a dependency on a live website.
2. Make the smallest useful change in the existing modules. Preserve complete CSV profiles, saved-plan compatibility, and response identities.
3. Run the focused tests, then the relevant full checks. Use local fixtures or a mocked HTTP client. Never submit to unrelated third-party forms as a test.
4. Check errors and recovery as well as the successful path. A click or redirect is not proof of a saved response.

From an environment with the project's development dependencies installed:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Package changes also need a wheel/sdist build, metadata checks, and an installed-wheel smoke test outside the checkout. CI already builds distributions without publishing them.

## Review and GitHub

When the user authorizes Git actions, make meaningful commits and open a focused PR linked to the issue. Explain what failed, what now happens, how it was checked, and any remaining limitation. Keep titles and descriptions short. Do not include credentials, real response data, local logs, browser profiles, or generated runtime files.

Let CI run and address the review when the user approves remote changes. Commits, pushes, PR or issue changes, merges, tags, branch deletion, and publication each require explicit user approval. Do not rewrite or manufacture history to fill a contribution graph.

Use repository metadata that accurately describes the tool. Add automation because it catches a real failure or saves maintenance, not because it looks advanced. Keep publication out of ordinary push, PR, merge, and tag workflows.

## Release

Prepare the version, changelog, tested source, and exact distribution files for review. Publishing is a separate user decision, including TestPyPI and GitHub releases. Never upload because a token exists or tests passed. Follow `AGENTS.md` for release and credential rules.

Current scope is anonymous public forms. Detect and reject account restrictions and file uploads; do not add authentication or transfer workflows.
