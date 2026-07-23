---
name: add-confluence-incident-evidence
description: Add safe, best-effort Confluence Cloud evidence retrieval to an existing incident-analysis backend. Use when implementing or porting Confluence CQL search, page-content retrieval, structured source citations, recommendation grounding, temporary-session retention, configuration, documentation, and tests for an incident analysis API such as POST /api/v1/incidents/analyze.
---

# Add Confluence Incident Evidence

Integrate Confluence as a read-only evidence source without making incident analysis depend on Confluence availability. Adapt names and framework conventions to the target repository; preserve its existing API fields and architecture.

Read [references/implementation-contract.md](references/implementation-contract.md) before editing. It defines the source model, status contract, REST calls, query rules, limits, failure behavior, session behavior, and test matrix.

## Workflow

### 1. Inspect the target

- Find the incident request/response models, analysis endpoint, orchestration service, LLM advisor or prompt builder, configuration loader, session/chat storage, tests, and documentation.
- Identify the language, async HTTP client, serialization conventions, dependency-injection pattern, and error-handling conventions.
- Inspect the working tree and preserve unrelated user changes.
- Search for an existing Confluence client before adding another one.

### 2. Plan additive integration points

- Add a `ConfluenceSource` domain model and an empty-by-default `confluenceSources` collection to the analysis response.
- Add a repository interface so orchestration depends on an abstraction, not an HTTP implementation.
- Add one Confluence evidence step before recommendation generation.
- Feed successful Confluence evidence to the existing advisor through its normal evidence channel.
- Save sources and findings in the initial analysis session for follow-up chat.
- Keep request payloads and existing response fields unchanged.

Do not introduce MCP unless the user explicitly requests it. The baseline implementation calls Confluence Cloud REST APIs directly.

### 3. Implement configuration securely

- Support `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`, `CONFLUENCE_SPACE_KEYS`, and optional `CONFLUENCE_RESULT_LIMIT`.
- Parse space keys as a trimmed comma-separated allow-list.
- Default the result limit to 3 and clamp it to 1–10.
- Enable the integration only when URL, email, token, and at least one allowed space are present.
- Keep Confluence optional at application startup.
- Put example placeholders in `.env.example`; never put a real token in source, examples, logs, exceptions, tests, or commits.
- Ensure local secret files are ignored by version control.

### 4. Implement read-only retrieval

- Use an asynchronous HTTP client, explicit request timeouts, JSON accept headers, and Basic authentication with Atlassian email plus API token.
- Build CQL only from incident title, service, and symptoms. Never include raw incident logs.
- Normalize whitespace, bound each term, escape CQL special characters, and include the configured space allow-list in every query.
- Search only pages through the Confluence v1 CQL search endpoint.
- Retrieve rendered page content through the Confluence v2 page endpoint using `body-format=view`.
- Fetch no more than the configured result limit.
- Convert HTML to readable plain text, normalize whitespace, decode entities, and truncate every excerpt to 4,000 characters.
- Reject sources outside the allow-list even if Confluence returns them.
- Return canonical page metadata and a usable absolute URL.

### 5. Add deterministic finding states

Always add one `ConfluenceKnowledgeAgent` finding:

- `CONFLUENCE_EVIDENCE_FOUND` when at least one usable page is returned.
- `NO_CONFLUENCE_EVIDENCE_FOUND` when search succeeds with no usable matches.
- `CONFLUENCE_NOT_CONFIGURED` when configuration is incomplete or invalid.
- `CONFLUENCE_BACKEND_UNAVAILABLE` for authentication, authorization, timeout, rate-limit, malformed response, page-retrieval, or server failures.

Bound externally visible error text and never expose credentials or raw response bodies. Continue normal incident analysis for every non-success state.

### 6. Ground recommendations and chat

- Represent each page as a structured source and also summarize its bounded excerpt in the Confluence agent finding.
- Pass that finding alongside historical, deployment, and code evidence to the advisor.
- Instruct the advisor to treat sources as evidence, distinguish evidence from hypotheses, and acknowledge insufficiency.
- Store Confluence sources in the temporary analysis session.
- Include page ID, title, URL, and excerpt in follow-up chat context and include URLs in chat citations.
- Reuse the initial snapshot during chat; do not add on-demand Confluence retrieval unless separately requested.

### 7. Test at repository, service, API, and session levels

- Mock all Atlassian HTTP calls; do not make live Confluence requests in automated tests.
- Assert CQL contains every allowed-space restriction and excludes logs and test credentials.
- Test escaping, bounds, HTML conversion, entity decoding, 4,000-character truncation, result limits, source mapping, and allow-list rejection.
- Test no results, incomplete configuration, timeout, 401/403, 429, malformed JSON/schema, partial page failures, and 5xx responses.
- Assert all failures retain a normal analysis response with an empty source list and the correct finding.
- Assert the advisor receives the Confluence finding.
- Assert sources survive session creation and appear in follow-up context/citations.
- Assert camelCase or the target API’s established serialization convention.
- Assert older analyses without the new field deserialize or serialize with an empty list.
- Run focused tests, then the complete test suite.

### 8. Finish safely

- Update environment documentation and the endpoint response example.
- Describe that this is REST-based, read-only, allow-listed, bounded, and best-effort.
- Report modified files, tests run, and any target-specific assumptions.
- Never report live credentials. If a token was exposed during development, advise revocation and replacement.
