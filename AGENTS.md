# GFormBot: instructions for coding agents

These instructions apply to this project. They override unrelated inherited challenge instructions. This is the GFormBot Python CLI, not a deletion-audit challenge.

## Publishing is always the user's decision

- Never publish to PyPI, TestPyPI, another package registry, or an installer/release distribution service unless the user explicitly requests that specific publication.
- Never create or publish a release, push a release tag, trigger a publishing workflow, or upload release artifacts without that request.
- An available API token, configured credentials, passing CI, a version bump, or a request to prepare a release is not permission to publish.
- Do not add automatic publishing on pushes, tags, or merges. Any future publishing workflow must require a deliberate user action and approval.
- Do not commit, push, or merge unless the user explicitly asks. Local preparation, inspection, builds, and tests are separate from publication.
- Never put tokens, passwords, cookies, or browser profiles in source files, logs, issue bodies, saved response files, or chat output. Configure credentials through an appropriate secret store when the user asks for setup.

## Project direction

- Fix form compatibility before changing the Selenium distribution: access/sign-in and response limits, answer-based section routing, and follow-up instructions are tracked in GitHub issues #2, #3, and #4.
- Implement and verify one agreed slice at a time. Reuse the existing parser, validation, browser factory, runner, and recovery journal. Preserve complete imported CSV profiles and exact saved-response identities.
- Keep working changes, including the local 0.3.1 command update, intact. Inspect status and diffs before editing; do not discard unrelated work.
- The size goal is a barebones Windows Selenium distribution containing the features actually used by this program plus their necessary runtime/import dependencies. Measure before removing anything. Retain required upstream notices and make updates reproducible. Broader operating-system packages are a later goal; do not remove the existing cross-platform implementation incidentally.
- Use ordinary, specific language for issues, PRs, and documentation. Do not rename existing issues unless the user requests it.

The active source is this repository. The sibling `google_form_test_bot_init` and earlier `gformbot` build are references; do not edit their application code unless the user changes that scope.

## Agreed Git workflow

- Follow `CONTRIBUTING.md`: one issue, one short branch, a failing regression case, a focused fix, local checks, a linked PR, CI/review, and a separately authorized merge and release.
- Use the existing default branch (`master`) as the integration target. Do not introduce a permanent develop branch or rewrite history for appearance.
- Preserve the uncommitted 0.3.1 update before starting compatibility work. Do not combine it with a new issue fix.
- Commit, push, open a PR, merge, or publish only within the user's explicit authorization for that action. An instruction to follow this workflow does not itself request those actions immediately.
- Write short, human titles and descriptions. Include the actual failure, resulting behavior, relevant verification, and remaining limits. Use truthful metadata and useful CI; do not manufacture activity or add badges implying unverified support.
- Implement issues in order: #2 access/sign-in/limits, #3 conditional sections, #4 follow-up instructions, then #5 Windows Selenium size. For #2, read `docs/form-access-plan.md` before editing and complete one reviewable slice at a time.
