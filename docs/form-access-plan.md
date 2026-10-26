# Issue #2: sign-in and response limits

Status: implemented on `fix/form-access`, separate from the preserved 0.3.1 update. Unit and local browser checks pass; real Google account sign-in still needs an owner-controlled check. Next: #3 branching, #4 follow-up instructions, then #5 Windows Selenium size.

## Original failures

- `inspector.py::inspect_form` reads anonymously with urllib. Its browser arguments do not create an authenticated session.
- `cli.py::_run_inspect` and `_command_answers` use that path. `execution.py::Runner.run` repeats anonymous inspection before starting its browser.
- `browser.py` forces private mode for every browser. Normal `generate` forces headless execution; background review creates a separate private viewer. A login prompt alone would still lose the session.
- `filler.py::_check_interstitial` handles only a few error states. After Submit, the wait checks confirmation/validation but misses access changes.
- Some review paths mark a row as reviewing before navigation. A blocked form can therefore look uncertain before the user could submit anything.

## Slice A: explain blocked forms

Extend the existing inspector/interstitial handling with one shared access result: usable, login needed, permission denied, already answered, closed/unpublished, temporary failure, or unknown.

Use final URL, response status, and actual page structure/content together. Missing form metadata alone is not proof of closure; words inside a question are not an access error. Keep public HTTP inspection fast.

Check initial reading, navigation, and submission outcomes. Stop blocked batches with clear next steps and preserve exact pending rows. A response already on the account is not proof this saved row was accepted. An unproven post-click outcome remains uncertain.

**Done:** all states have fixtures; public forms work unchanged; no needless generation for a known blocked form, false success, or automatic uncertain retry.

## Slice B: keep one authenticated session

Offer deliberate visible sign-in in a dedicated browser. The user authenticates directly; never ask for passwords or copy a personal Chrome profile. Start with an ephemeral session. Persistent dedicated-profile reuse can follow separately with locking/cleanup.

Parse the authenticated page through the existing `parse_form_html`. Carry the same owned session through reading, fingerprint recheck, filling, review, and confirmation. Replace both anonymous reinspection paths and the unrelated background viewer for authenticated runs. Public forms keep their existing lightweight path.

Thread the option through the wizard, `generate`, `prepare`, and saved JSON runs. Do not silently force an authenticated browser headless. Quiet mode must not unexpectedly open a login window. Cancel, timeout, wrong account, expired session, and rejected automated login must leave answers intact. Helpers must not quit a borrowed driver; its owner closes it once.

**Done:** a controlled login-required form works through one session; public mode still works; cancellation is safe; no auth material enters plans, logs, or AI prompts. Universal Google login support is not promised.

## Slice C: enforce limits and preserve recovery

Represent observed email/access/limit policy separately from ordinary answer fields. Verified email uses the real account and its consent control. Unrecognized limits stay unknown.

Reject a known incompatible multi-response count before generating/filling; explain one permitted response or an owner-controlled test copy. Never silently change the count or rotate accounts to evade the setting.

Recheck access between responses and at transitions. Keep stable original IDs and the existing `Session`/`Journal`. Correct review-state timing: failure before a form can be reviewed stays prepared; once manual submission was possible, remain conservative. Keep submit intent immediately before the click. Explicit rejection is different from uncertain acceptance.

**Done:** no false confirmation or duplicate retry; closure mid-batch retains the right pending rows; halt, manual wait/tabs, background, direct submission, and saved-plan resume obey the same rules.

## Verification

Extend existing tests in `test_inspector.py`, `test_filler.py`, `test_state.py`, `test_execution_recovery.py`, `test_commands.py`, and `test_halt.py`. Use local access/session-expiry fixtures, then Windows browser checks and owner-controlled Google Forms. Cover a closed form before/mid-run, wrong account/domain, already answered, one-response limit, session expiry during Next, timeout after Submit, and manual tab closure.

Review each slice before proceeding. No parser rewrite, authentication service, cookie export, AI browser framework, or Selenium trim is needed for this issue.
