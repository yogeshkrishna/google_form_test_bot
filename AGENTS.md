# GFormBot: instructions for coding agents

These instructions apply to this project. They override unrelated inherited challenge instructions. This is the GFormBot Python CLI, not a deletion-audit challenge.

## Publishing is always the user's decision

- Never publish to PyPI, TestPyPI, another package registry, or an installer/release distribution service unless the user explicitly requests that specific publication.
- Never create or publish a release, push a release tag, trigger a publishing workflow, or upload release artifacts without that request.
- An available API token, configured credentials, passing CI, a version bump, or a request to prepare a release is not permission to publish.
- Do not add automatic publishing on pushes, tags, or merges. Any future publishing workflow must require a deliberate user action and approval.
- The user authorizes commits, pushes, PR creation, and merges for agreed project work. Keep fixes focused and tested. Package publication still requires an explicit request.
- Never put tokens, passwords, cookies, or browser profiles in source files, logs, issue bodies, saved response files, or chat output. Configure credentials through an appropriate secret store when the user asks for setup.

## Project direction

- Support anonymous public forms. Automatic submission uses direct HTTP; visible browsers are for manual review and the unchanged ChatGPT generation backend. Google-account sign-in and file transfers are outside the product scope; detect restrictions and report them clearly.
- Implement and verify one agreed slice at a time. Reuse the existing parser, validation, browser factory, runner, and recovery journal. Preserve complete imported CSV profiles and exact saved-response identities.
- Inspect status and diffs before editing; preserve unrelated work and private audit evidence.
- The size goal is a barebones Windows Selenium distribution containing the features actually used by this program plus their necessary runtime/import dependencies. Measure before removing anything. Retain required upstream notices and make updates reproducible. Broader operating-system packages are a later goal; do not remove the existing cross-platform implementation incidentally.
- Use ordinary, specific language for issues, PRs, and documentation. Do not rename existing issues unless the user requests it.
- Propose new runtime dependencies or departures from the agreed approach before implementing them.

The active source is this repository. The sibling `google_form_test_bot_init` and earlier `gformbot` build are references; do not edit their application code unless the user changes that scope.

## Agreed Git workflow

- Follow `CONTRIBUTING.md`: one issue, one short branch, a failing regression case, a focused fix, local checks, a linked PR, CI/review, merge, and a separately authorized publication.
- Use the existing default branch (`master`) as the integration target. Do not introduce a permanent develop branch or rewrite history for appearance.
- Preserve saved-plan fingerprints and batch identities. Confirmed and uncertain outcomes must never become automatically retryable after migration or cleanup.
- Use the standing Git authorization above within agreed project scope. It does not authorize package or release publication.
- Write short, human titles and descriptions. Include the actual failure, resulting behavior, relevant verification, and remaining limits. Use truthful metadata and useful CI; do not manufacture activity or add badges implying unverified support.
- Unknown routing must fail closed. Never silently skip an unsupported question on the active route or invent an answer.
