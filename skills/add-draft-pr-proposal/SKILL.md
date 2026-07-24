---
name: add-draft-pr-proposal
description: Add the ProdPulse incident-session draft pull-request workflow to a FastAPI backend. Use when porting the staged GitHub file-read, AI single-file diff preview, session-scoped preview, and user-confirmed draft-PR creation endpoints after updating from main.
---

# Add Draft PR Proposal

Port the staged draft-PR capability into the current target branch. Preserve unrelated changes and fit the target's conventions. Read [references/implementation-contract.md](references/implementation-contract.md) before editing.

## Workflow

1. Inspect the current analysis-session models, API routes, startup wiring, settings, Azure OpenAI client, and test layout. Check the working tree before editing.
2. Add request/response models for a preview (`repository`, `filePath`, optional `baseBranch`) and creation (`previewId`). Keep the target's existing alias convention.
3. Add a session-scoped preview store with the same lifetime as the analysis session. A preview must retain repository, base branch, file path, and validated patch. Expire it with its session.
4. Add a GitHub REST adapter that validates `owner/repository` names and repository-relative paths, reads the selected UTF-8 file and branch, and sends GitHub API version and bearer-token headers.
5. Generate exactly one unified diff from the existing code-change recommendation and current file content. Require only `--- a/<path>`, `+++ b/<path>`, and valid hunks; reject prose, Markdown, new files, multiple files, unsafe paths, mismatched paths, and stale context.
6. Implement `POST /api/v1/analysis-sessions/{analysisId}/draft-pr/preview`. It must create no repository state: read the file, generate and validate the diff, apply it locally to prove it is usable, and return/store the preview.
7. Implement `POST /api/v1/analysis-sessions/{analysisId}/draft-pr`. Require a stored preview and explicitly treat this endpoint as the sole write boundary. Re-read the base file, re-validate/re-apply the preview patch, create a unique `incident/<sanitized-id>-draft[-N]` branch, update that one file, and create a draft PR. Never update an existing branch or create a non-draft PR.
8. Load local `.env` from the repository root without overriding deployment-provided variables. Accept `GITHUB_TOKEN` first and `GITHUB_PAT` as a local-development fallback. Do not expose secrets in errors, logs, source, examples, or tests.
9. Return 404 for unknown/expired sessions or previews; return bounded 400 errors for invalid configuration, paths, patches, and GitHub permission failures; use the target's existing 503 convention for temporary upstream failures.
10. Add unit tests for diff application, stale context rejection, multi-file rejection, and selected-path mismatch. Run focused tests and the full suite.

## Safety requirements

- Do not call GitHub write endpoints from preview handling.
- Do not write local repository files.
- Do not create a branch, commit, pull request, comment, or deployment until the explicit create endpoint is called with a preview ID.
- Limit writes to the one selected existing file and a new uniquely named branch.
- Do not infer a repository from an incident; require the caller to provide it.

## Compatibility

This staged feature consumes a string `codeChanges` recommendation. If the target branch instead uses a structured code-change object, pass its focused `suggested_code_changes` field to the diff generator and retain the source metadata separately; do not serialize the object itself into the diff prompt.
