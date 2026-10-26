# Working on GFormBot

Keep changes small enough to explain, test, and review.

## Before changing code

Check existing issues and `git status`. Describe the failure, expected behavior, and a small reproduction. Agree on the scope before adding a feature or changing the architecture. Preserve any existing working changes.

Use one short branch per fix, for example `fix/form-access`, based on the current default branch (`master`). Keep the existing 0.3.1 command update separate from new compatibility work. Creating a branch does not require discarding unfinished work.

## Build the fix

1. Add a regression case that fails for the reported problem. Prefer sanitized form metadata and local HTML fixtures over a dependency on a live website.
2. Make the smallest useful change in the existing modules. Preserve complete CSV profiles, saved-plan compatibility, and response identities.
3. Run the focused tests, then the relevant full checks. Add browser coverage when interaction changes. Never submit to unrelated third-party forms as a test.
4. Check errors and recovery as well as the successful path. A click or redirect is not proof of a saved response.

From an environment with the project's development dependencies installed:

```powershell
python -m pytest -q -m "not browser"
python -m ruff check .
python -m ruff format --check .
```

For local Chrome fixtures:

```powershell
$env:GFORMBOT_BROWSER_TESTS = '1'
python -m pytest -q -m browser
Remove-Item Env:GFORMBOT_BROWSER_TESTS
```

Package changes also need a wheel/sdist build, metadata checks, and an installed-wheel smoke test outside the checkout. CI already builds distributions without publishing them.

## Review and GitHub

When the user authorizes Git actions, make meaningful commits and open a focused PR linked to the issue. Explain what failed, what now happens, how it was checked, and any remaining limitation. Keep titles and descriptions short. Do not include credentials, real response data, local logs, browser profiles, or generated runtime files.

Let CI run and address the review. The user authorizes Git actions for agreed project work; package publication remains a separate decision. Delete the finished feature branch when appropriate; do not rewrite or manufacture history to fill a contribution graph.

Use repository metadata that accurately describes the tool. Add automation because it catches a real failure or saves maintenance, not because it looks advanced. Keep publication out of ordinary push, PR, merge, and tag workflows.

## Release

Prepare the version, changelog, tested source, and exact distribution files for review. Publishing is a separate user decision, including TestPyPI and GitHub releases. Never upload because a token exists or tests passed. Follow `AGENTS.md` for release and credential rules.

Current compatibility order: #2, #3, #4, then #5. The first implementation plan is in `docs/form-access-plan.md`.
