# Confluence Incident Evidence Implementation Contract

## Contents

1. Runtime contract
2. API contract
3. Retrieval algorithm
4. CQL construction
5. Content mapping
6. Orchestration and advisor flow
7. Session flow
8. Failure semantics
9. Test matrix
10. Porting checklist

## 1. Runtime contract

Configuration:

| Variable | Required to enable | Behavior |
|---|---:|---|
| `CONFLUENCE_BASE_URL` | Yes | Atlassian site root, for example `https://company.atlassian.net` |
| `CONFLUENCE_EMAIL` | Yes | Atlassian service-account email |
| `CONFLUENCE_API_TOKEN` | Yes | Secret API token; never log or commit |
| `CONFLUENCE_SPACE_KEYS` | Yes | Comma-separated allow-list |
| `CONFLUENCE_RESULT_LIMIT` | No | Default 3, minimum 1, maximum 10 |

Use Basic authentication with email as username and API token as password. Use an explicit timeout, such as 20 seconds per request. Treat the repository as disabled when any required value is absent.

Do not hardcode a real URL, email, token, or organization-specific space key in a portable implementation. Use placeholders in examples.

## 2. API contract

Add this source shape using the target project’s naming and type system:

```json
{
  "pageId": "123456",
  "title": "Payment processing runbook",
  "url": "https://company.atlassian.net/wiki/spaces/PAY/pages/123456",
  "spaceKey": "PAY",
  "lastModified": "2026-07-20T10:30:00Z",
  "excerpt": "Bounded plain-text page content..."
}
```

Fields:

- `pageId`: required string
- `title`: required string
- `url`: required absolute string
- `spaceKey`: required allowed space key
- `lastModified`: optional timestamp
- `excerpt`: required non-empty plain text, at most 4,000 characters

Add an additive `confluenceSources` array to the incident-analysis response. Default it to an empty list so existing callers and stored analyses remain compatible.

Example:

```json
{
  "analysisId": "analysis-...",
  "incomingIncident": {},
  "similarIncidents": [],
  "agentFindings": [],
  "confluenceSources": [],
  "recommendation": "..."
}
```

Do not change the incident-analysis request schema.

## 3. Retrieval algorithm

Use this sequence:

```text
incident
  -> build CQL from title, service, symptoms
  -> GET /wiki/rest/api/search
  -> validate results list
  -> for each bounded result:
       GET /wiki/api/v2/pages/{pageId}?body-format=view
       validate metadata and allowed space
       rendered HTML -> plain text -> first 4,000 characters
       create ConfluenceSource
  -> return usable sources
```

Search endpoint:

```http
GET /wiki/rest/api/search?cql=<encoded-cql>&limit=<bounded-limit>
Accept: application/json
Authorization: Basic <email:token>
```

Page endpoint:

```http
GET /wiki/api/v2/pages/{pageId}?body-format=view
Accept: application/json
Authorization: Basic <email:token>
```

Limit source count to the configured value after clamping it to 1–10. Never return or prompt with full page bodies.

If one page fetch fails but other valid pages exist, retain the valid pages. If page matches existed but no page can be retrieved or mapped because of backend/response failures, surface backend unavailable rather than falsely reporting no evidence.

## 4. CQL construction

Construct:

```text
type = page
AND space IN ("OPS", "PAY")
AND (
  text ~ "<normalized and escaped title>"
  OR text ~ "<normalized and escaped service>"
  OR text ~ "<normalized and escaped symptoms>"
)
```

Rules:

1. Use only title, service, and symptoms.
2. Never concatenate raw logs into the search.
3. Collapse whitespace and trim each term.
4. Bound each term, for example to 500 characters.
5. Escape CQL/Lucene special characters including `+ - & | ! ( ) { } [ ] ^ " ~ * ? : \ /`.
6. Omit empty terms.
7. Always add `type = page`.
8. Always add `space IN (...)` from validated configured keys.
9. Validate space keys against a conservative pattern such as `[A-Za-z0-9_-]+`.
10. Fail closed if no valid space key or no usable search term exists.

This is normalization and CQL escaping, not secret detection. Advise callers not to place credentials in title, service, or symptoms.

## 5. Content mapping

For each result:

- Obtain page ID from page response or search result.
- Prefer the page response title, then fall back to search metadata.
- Obtain space key from result metadata, search URL, or another verified API field.
- If only one space is configured, it may be used as a fallback only because the query was already restricted to that space.
- Reject the page if the resolved space key is not allow-listed.
- Prefer the page `_links.webui` URL, then a search-result URL, then a page-ID fallback.
- Resolve relative URLs against the configured Atlassian base URL.
- Parse `lastModified` from the best available version/search timestamp; return null if invalid.
- Read rendered content from the v2 `body.view` representation.
- Strip markup with a real HTML parser, collect text nodes, decode HTML entities, normalize whitespace, trim, and truncate to 4,000 characters.
- Reject empty excerpts.

## 6. Orchestration and advisor flow

Run Confluence retrieval before recommendation generation:

```text
historical retrieval
deployment evidence
optional code evidence
Confluence retrieval
advisor recommendation
response/session creation
```

Create exactly one finding:

```json
{
  "agentName": "ConfluenceKnowledgeAgent",
  "status": "CONFLUENCE_EVIDENCE_FOUND",
  "summary": "Found 1 relevant page(s) in approved Confluence spaces.",
  "evidence": "pageId=123456, title=..., url=..., spaceKey=PAY, excerpt=..."
}
```

Join multiple bounded source summaries when pages match. Supply the finding through the same evidence collection consumed by the existing advisor. Avoid a parallel prompt path that could drift from established grounding and safety instructions.

Ensure the advisor prompt:

- Uses only supplied evidence.
- Treats retrieval as evidence, not proof of root cause.
- Labels likely causes as hypotheses.
- Gives safe investigation/remediation steps.
- States when evidence is insufficient.

Return both `agentFindings` and structured `confluenceSources`.

## 7. Session flow

When creating a temporary analysis session, copy:

- Original incident
- Historical matches
- All agent findings
- Initial recommendation
- Structured Confluence sources

Build follow-up chat context from source page ID, title, URL, and bounded excerpt. Add source URLs to the chat response citation/source list. Preserve the session’s established TTL and deletion behavior.

Do not perform a new Confluence search during follow-up chat unless the user separately asks for an on-demand Confluence tool.

## 8. Failure semantics

| Condition | Status | Sources | Overall analysis |
|---|---|---:|---|
| At least one usable page | `CONFLUENCE_EVIDENCE_FOUND` | Populated | Continue |
| Successful search, no usable match | `NO_CONFLUENCE_EVIDENCE_FOUND` | Empty | Continue |
| Missing/invalid optional configuration | `CONFLUENCE_NOT_CONFIGURED` | Empty | Continue |
| Timeout or transport error | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |
| HTTP 401/403 | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |
| HTTP 429 | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |
| HTTP 5xx | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |
| Invalid JSON/schema | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |
| All matched page fetches fail | `CONFLUENCE_BACKEND_UNAVAILABLE` | Empty | Continue |

Translate HTTP failures into bounded generic messages such as `Confluence request failed with HTTP 403.` Do not include response bodies, headers, authorization material, tokens, or stack traces in the API response. Bound finding error evidence, for example to 500 characters.

## 9. Test matrix

Repository tests:

- Multiple allowed spaces are present in CQL.
- Title, service, and symptoms are present.
- Logs and sample credentials are absent.
- Special characters are escaped.
- Result limit defaults and clamps correctly.
- Search endpoint and page endpoint are called with expected parameters.
- Basic authentication is configured without logging credentials.
- Rendered HTML becomes normalized text.
- Entities are decoded.
- Excerpt length is at most 4,000 characters.
- Metadata maps to the source contract.
- Relative URLs become absolute.
- Returned non-allow-listed spaces are rejected.
- Missing result list or malformed page body is handled.
- Partial page failure retains successful sources.
- Total page failure raises a bounded repository error.

Service tests:

- Configured match produces `CONFLUENCE_EVIDENCE_FOUND`.
- Successful empty search produces `NO_CONFLUENCE_EVIDENCE_FOUND`.
- Missing repository produces `CONFLUENCE_NOT_CONFIGURED`.
- Repository exception produces `CONFLUENCE_BACKEND_UNAVAILABLE`.
- Advisor receives the Confluence finding and excerpt.
- Normal recommendation still returns on every Confluence failure.

API/model tests:

- `confluenceSources` serializes using established public casing.
- Existing fields and request schema remain unchanged.
- Missing sources default to `[]`.
- Optional `lastModified` accepts null.

Session/chat tests:

- Sources and excerpts survive session creation.
- Follow-up system context contains Confluence evidence.
- Chat sources contain the page URL.
- No on-demand Confluence tool is registered accidentally.

Run focused tests first, then the complete suite.

## 10. Porting checklist

- [ ] Locate domain models and add the source model.
- [ ] Add empty-by-default response field.
- [ ] Add all configuration variables and safe parsing.
- [ ] Add an async repository abstraction and REST implementation.
- [ ] Add allow-listed CQL construction without logs.
- [ ] Add bounded page retrieval and HTML-to-text conversion.
- [ ] Add all four finding states.
- [ ] Feed the finding to the advisor before generation.
- [ ] Return structured sources.
- [ ] Retain sources in analysis sessions and chat citations.
- [ ] Document direct REST architecture and runtime prerequisites.
- [ ] Add repository, service, API, serialization, and session tests.
- [ ] Run the complete test suite.
- [ ] Confirm no secret or organization-specific credential was committed.
