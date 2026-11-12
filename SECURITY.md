# Security reports

Use GitHub's private vulnerability reporting when available. Otherwise open an issue describing the impact without credentials, private form links, response data or file paths, and request a private channel before sharing sensitive details.

Use the current GitHub build for unreleased fixes. Saved responses and run logs contain plaintext answers; keep them in a private directory. Do not commit cookies, browser profiles, credentials or real response data.

AI generation sends form text, user instructions, and reference examples to ChatGPT's guest service over HTTPS. Later prompts can also include relevant accepted answers for consistency and review. Image and video metadata stays in form inspection. Any details entered in extra instructions are sent as text. Supplied-data submission runs independently of ChatGPT.

Known secret URL parameters are redacted in outgoing AI prompts, including corrections. Embedded credentials and account or edit-response links are omitted. Check ordinary form text and instructions for private details before generation. Local configurations, generation drafts, and prepared responses store metadata and answers in plaintext. Protect these files along with run logs and the submission journal. Form validation checks answer structure and constraints. Review generated claims separately when factual accuracy matters.

The response editor is a temporary server bound to 127.0.0.1 with a random session URL, same-origin save checks, no-store responses and a restricted content policy. It sends no data to AI or Google. Saved edits keep batch identities and cannot reset submitted or uncertain outcomes. It closes on Done, terminal Ctrl+C or an idle timeout.
