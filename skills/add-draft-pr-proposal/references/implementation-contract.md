# Draft PR Proposal Contract

## Endpoints

`POST /api/v1/analysis-sessions/{analysisId}/draft-pr/preview`

```json
{"repository":"owner/repository","filePath":"src/Service.java","baseBranch":"main"}
```

Return a session-local preview:

```json
{"previewId":"draft-...","repository":"owner/repository","baseBranch":"main","filePath":"src/Service.java","patch":"--- a/src/Service.java\n+++ b/src/Service.java\n..."}
```

`POST /api/v1/analysis-sessions/{analysisId}/draft-pr`

```json
{"previewId":"draft-..."}
```

Return the draft PR URL, number, and created branch.

## GitHub behavior

Use `GET /repos/{owner}/{repo}` to resolve the default branch, `GET /repos/{owner}/{repo}/contents/{path}?ref={branch}` to read the source, and only on explicit create: `GET /git/ref/heads/{branch}`, `POST /git/refs`, `PUT /contents/{path}`, then `POST /pulls` with `draft: true`.

Validate `owner/repository` against `[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+`. Reject blank paths, backslashes, absolute paths, and `..` components. Require base64 UTF-8 GitHub content.

## Diff handling

Require one existing-file unified diff. If headers are present they must be exactly `--- a/<path>` and `+++ b/<path>` for the selected path. Reject `/dev/null`, multiple header pairs, path traversal, unsupported records, invalid hunks, and context that no longer matches the freshly read file. Apply the diff in memory before previewing and again immediately before GitHub writes.

## Porting checklist

- Add `DraftPrPreviewRequest`, `DraftPrCreateRequest`, `DraftPrPreviewResponse`, and `DraftPrCreateResponse`.
- Add `draft_previews` plus save/get methods to the temporary analysis session.
- Add `GithubDraftPrService` and `DraftPrProposalService`.
- Wire configuration and services at application startup.
- Add preview/create routes after chat routes.
- Keep the existing incident analysis and chat response contracts unchanged.
- Test all patch validation paths without live GitHub calls.
