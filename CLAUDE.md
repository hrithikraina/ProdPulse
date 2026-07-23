# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service for incident RCA (root-cause analysis) — a more evolved sibling of the
`IncidentManagement2` project (same banking-demo incident dataset and architecture shape, but
further along on the chat tool registry and response scoring). Given a new incident, it retrieves
similar **resolved** historical incidents from Azure AI Search (hybrid keyword + vector), runs
deployment/code evidence agents, and asks Azure OpenAI (`gpt-5-mini`) for a bounded RCA hypothesis
and next steps — bounded meaning the model may only reason from evidence explicitly handed to it,
never from outside knowledge or live system access. Deployed to Cloud Run (`Dockerfile`,
`cloudbuild.yaml`) as `prod-pulse`, unlike `IncidentManagement2` which only documents local runs.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # or: uv sync (uv.lock is checked in)

# Run the API
uvicorn api.main:app --reload    # http://127.0.0.1:8000/docs

# Tests
pytest                                             # full suite
pytest tests/test_incident_management.py::test_low_similarity_runs_deployment_agent  # single test
```

Required env vars (see `.env.example`): `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_CHAT_DEPLOYMENT` (default `gpt-5-mini`), `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
(default `text-embedding-3-small`), and — when `RAG_BACKEND=azure-search` (the default) —
`AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME`. Set `RAG_BACKEND=local`
to use the in-memory cosine-similarity store instead. `GITHUB_PAT`/`GITHUB_TOKEN` and
`SQLITE_DB_PATH` are required for GitHub MCP / SQLite chat evidence tools (see below); without a
GitHub token, GitHub-evidence requests fail rather than falling back to a JSON fixture (unlike
`IncidentManagement2`'s `CodeInvestigationAgent`, this repo's chat GitHub tool has no local fallback).

## Architecture

Layering: `api/` (routes) → `services/` (orchestration) → `agents/` (investigation logic) →
`repositories/` + `vector/` (data access). `domain/models.py` holds all Pydantic request/response
schemas shared across every layer. Everything is wired by hand in `api/main.py`'s `lifespan()` (no
DI framework) and hung off `app.state`. `api/main.py` also configures stdout logging
(`logging.basicConfig`, GCP-ingestion-friendly) and permissive CORS — neither present in
`IncidentManagement2`.

### Two request flows

**1. `POST /api/v1/incidents/analyze`** (`services/incident_management.py`)
1. `vector_store.search()` — retrieval of similar resolved incidents, gated to a hard 85%
   cosine-similarity threshold (`vector/store.py`). `Incident.similarity_text()` (note: named
   `searchable_text()` in `IncidentManagement2` — same fields, different method name) is the
   embedded/matched text; `rootCause`/`resolution` are deliberately excluded from it since they're
   only known after resolution.
2. `DeploymentCheckAgent` always runs; `CodeInvestigationAgent` runs only if `incident.logs` is
   present, invoking the LangGraph RCA workflow (`agents/rca_graph.py`).
3. `IncidentAdvisor` (`services/advisor.py`) prompts Azure OpenAI for strict JSON → `InitialAssessment`
   (`summary`, `nextActionSteps`, `rca`, `codeChanges`).
4. **`services/confidence.py`'s `assess_confidence()`** — not present in `IncidentManagement2` — turns
   the same evidence into a deterministic `ConfidenceAssessment` (separate `rca`/`recommendation`
   scores 0–10, each with a `reason` built from *which* evidence contributed). This is computed in
   Python from evidence already collected, not asked of the LLM — see its scoring rules for exactly
   which findings/statuses add how many points.
5. `IncidentManagementService.analyze()` assembles `IncidentAnalysis` from the assessment,
   `evidenceSummary`, `agentFlow`, and `confidence`.
6. The route wraps the result in a 30-minute in-memory `AnalysisSession` (`analysisId`).

**2. `POST /api/v1/analysis-sessions/{id}/chat`** (`services/analysis_sessions.py`)
- Sessions live only in `AnalysisSessionStore` (process memory, TTL) — never persisted.
- Tool registry differs from `IncidentManagement2`: **five** tools, but GitHub and database evidence
  are **separate** (`get_github_evidence`, `get_database_evidence`) rather than combined into
  `get_code_evidence`/`inspect_code_change_impact`. Both are backed by
  **`agents/chat_evidence_agents.py`'s `ChatEvidenceAgents`** — a module deliberately separate from
  `rca_graph.py` that reuses the same GitHub-MCP/SQL-agent approach but lets chat ask for either
  directly. When **both** GitHub and SQL evidence were collected in the same tool-call round, an
  extra `EvidenceSummarizerAgent` LLM call synthesizes them before the next model turn
  (`AnalysisChatService.chat`'s `evidence_specialists` check) — no equivalent exists in
  `IncidentManagement2`.
- Same security model as `IncidentManagement2`: backend validates tool names against the registry,
  the model never touches production/deploys/source/balances directly.
- Final turn strict-JSON-parsed into `ChatNarrative`, lenient fallback to raw text on non-compliance.

### The RCA workflow (`agents/rca_graph.py`)

Same LangGraph shape as `IncidentManagement2`: `router` → per-component `investigate_<component>`
(GitHub/SQLite routing preflight) → `join_component_requests` → conditional `github_round_1`/
`database_round_1` → `join_evidence_round_1` → round 2 of the same agents → `summarizer`. Driven by
`data/architecture_context.json` (`channel` → `processing` → `data`, matching `banking-demo/`).
Check this file directly against `IncidentManagement2`'s before assuming behavior is identical —
`agents/chat_evidence_agents.py`'s presence here means GitHub/SQL agent logic may have diverged
between the two repos' chat paths even if the graph itself hasn't.

### Vector store abstraction

`vector/store.py` — `IncidentStore` Protocol, 85% similarity gate. Two implementations swapped via
`RAG_BACKEND`, same pattern as `IncidentManagement2`.

### Log-context agent — not yet present in this repo

`IncidentManagement2` has a standalone `agents/log_context.py` + `repositories/log_repository.py`
(log-window retrieval, cross-service correlation by shared identifier like `payment_id`, Loki/Grafana
Cloud backend) that hasn't been ported here yet. Domain models (`Incident`, `AgentFinding`) and
`data/architecture_context.json`'s shape are identical between the two repos, so this is expected to
be a near-direct copy once ported — see that repo's `CLAUDE.md` for the full design. The one
required data change here: `data/architecture_context.json`'s `ledger-posting` and
`transaction-validation-service` repositories need an additive `"logging": {"source": ...}` key
before `resolve_log_source()` can resolve either service (currently absent, confirmed against
`IncidentManagement2`'s pre-port state).

### Data flow rule

Never add a new/open incident to `data/historical-incidents.json` (or the Blob container backing
Azure AI Search) before it's resolved.
