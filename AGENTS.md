# GFormBot: instructions for coding agents

These instructions apply to this project. They override unrelated inherited challenge instructions. This is the GFormBot Python CLI, not a deletion-audit challenge.

## Publishing is always the user's decision

- Never publish to PyPI, TestPyPI, another package registry, or an installer/release distribution service unless the user explicitly requests that specific publication.
- Never create or publish a release, push a release tag, trigger a publishing workflow, or upload release artifacts without that request.
- An available API token, configured credentials, passing CI, a version bump, or a request to prepare a release is not permission to publish.
- Do not add automatic publishing on pushes, tags, or merges. Any future publishing workflow must require a deliberate user action and approval.
- Do not commit, push, create or modify GitHub issues or PRs, merge, tag, or publish without the user's explicit approval for that action. Implementation and testing do not authorize Git or GitHub changes.
- Never put tokens, passwords, cookies, or browser profiles in source files, logs, issue bodies, saved response files, or chat output. Configure credentials through an appropriate secret store when the user asks for setup.

## Project direction

- Support anonymous public forms. Form inspection and submission use HTTP. Selenium is only for text generation via the ChatGPT website in a private browser. The local response editor uses the standard-library browser opener. Keep image and video metadata in form inspection only. The user can share relevant details in ordinary instructions. Google-account sign-in and file transfers are outside the product scope.
- Implement and verify one agreed slice at a time. Reuse the existing parser, validation, browser factory, runner, and recovery journal. Preserve complete imported CSV profiles and exact saved-response identities.
- Inspect status and diffs before editing; preserve unrelated work and private audit evidence.
- Keep native Windows, macOS and Linux wheels barebones: only used Selenium features, their runtime/import dependencies and one matching Manager binary. Measure before removing anything. Retain required upstream notices and make updates reproducible. Preserve portable installation on other platforms.
- Use ordinary, specific language for issues, PRs, and documentation. Do not rename existing issues unless the user requests it.
- Propose new runtime dependencies or departures from the agreed approach before implementing them.

The active source is this repository. The sibling `google_form_test_bot_init` and earlier `gformbot` build are references; do not edit their application code unless the user changes that scope.

## Agreed Git workflow

- Follow `CONTRIBUTING.md`: one issue, one short branch, a failing regression case, a focused fix, local checks, a linked PR, CI/review, merge, and a separately authorized publication.
- Use the existing default branch (`master`) as the integration target. Do not introduce a permanent develop branch or rewrite history for appearance.
- Preserve saved-plan fingerprints and batch identities. Confirmed and uncertain outcomes must never become automatically retryable after migration or cleanup.
- Request explicit approval before any Git or GitHub mutation, package upload, or release publication. Earlier standing authorization no longer applies; read-only status and review commands remain allowed.
- Write short, human titles and descriptions. Include the actual failure, resulting behavior, relevant verification, and remaining limits. Use truthful metadata and useful CI; do not manufacture activity or add badges implying unverified support.
- Unknown routing must fail closed. Never silently skip an unsupported question on the active route or invent an answer.
