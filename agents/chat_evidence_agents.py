"""Read-only GitHub MCP and SQLite evidence agents for follow-up chat.

This module is intentionally separate from rca_graph.py.  It reuses the same
MCP/SQL approach but lets the chat tool registry ask for GitHub or database
evidence directly without changing the RCA graph's flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import AzureChatOpenAI

from domain.models import AgentFinding, Incident


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
ARCHITECTURE = json.loads((PROJECT_ROOT / "data" / "architecture_context.json").read_text(encoding="utf-8"))
_MODEL: AzureChatOpenAI | None = None


def _model() -> AzureChatOpenAI:
    """Delay Azure configuration validation until chat evidence is requested."""
    global _MODEL
    if _MODEL is None:
        _MODEL = AzureChatOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini")),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            temperature=1
        )
    return _MODEL


class ChatEvidenceAgents:
    """Lazily initialized, read-only evidence specialists for one API process."""

    def __init__(self) -> None:
        self._github_client: MultiServerMCPClient | None = None
        self._github_agent: Any | None = None
        self._github_lock = asyncio.Lock()
        self._sql_agent: Any | None = None
        self._sql_path: Path | None = None
        self._sql_lock = asyncio.Lock()

    @staticmethod
    def _database_path() -> Path:
        return (PROJECT_ROOT / os.getenv("SQLITE_DB_PATH", "PaymentsPlatform.db")).resolve()

    async def _github(self) -> Any:
        if self._github_agent is not None:
            return self._github_agent
        async with self._github_lock:
            if self._github_agent is None:
                token = os.getenv("GITHUB_PAT") or os.getenv("GITHUB_TOKEN")
                if not token:
                    raise RuntimeError("Set GITHUB_PAT or GITHUB_TOKEN to collect GitHub MCP evidence.")
                self._github_client = MultiServerMCPClient({"github": {
                    "transport": "http", "url": "https://api.githubcopilot.com/mcp/",
                    "headers": {"Authorization": f"Bearer {token}"},
                }})
                self._github_agent = create_agent(_model(), await self._github_client.get_tools())
        return self._github_agent

    async def _sql(self) -> Any:
        path = self._database_path()
        if self._sql_agent is not None:
            if path != self._sql_path:
                raise RuntimeError("The SQL evidence agent is already initialized for a different database path.")
            return self._sql_agent
        async with self._sql_lock:
            if self._sql_agent is None:
                if not path.is_file():
                    raise RuntimeError(f"SQLite database not found: {path}")
                try:
                    from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
                    from langchain_community.utilities import SQLDatabase
                except ImportError as error:
                    raise RuntimeError("SQL evidence needs langchain-community and sqlalchemy.") from error
                database = SQLDatabase.from_uri(f"sqlite:///{path.as_posix()}")
                prefix = "You are a read-only SQLite incident-evidence assistant. Never use INSERT, UPDATE, DELETE, DROP, ALTER, or TRUNCATE. Explain factual results only."
                self._sql_agent = create_sql_agent(llm=_model(), toolkit=SQLDatabaseToolkit(db=database, llm=_model()), verbose=True, prefix=prefix, agent_type="openai-tools")
                self._sql_path = path
        return self._sql_agent

    @staticmethod
    def _github_targets(text: str) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        lowered = text.casefold()
        for component in ARCHITECTURE["components"].values():
            for repository in component.get("repositories", []):
                github = repository.get("github")
                terms = [repository.get("name", ""), *repository.get("keywords", [])]
                if github and any(term and term.casefold() in lowered for term in terms):
                    targets.append({"owner": github["owner"], "repo": github["repo"], "branch": github.get("branch", "main")})
        # The architecture currently has one MCP-enabled repository. Use it
        # when the user asks a general code question without naming it.
        if not targets:
            for component in ARCHITECTURE["components"].values():
                for repository in component.get("repositories", []):
                    if github := repository.get("github"):
                        targets.append({"owner": github["owner"], "repo": github["repo"], "branch": github.get("branch", "main")})
        return targets

    async def github_evidence(self, incident: Incident, focus: str) -> tuple[AgentFinding, list[str]]:
        targets = self._github_targets(" ".join((incident.service, incident.title, incident.symptoms, incident.logs or "", focus)))
        reports: list[dict[str, str]] = []
        agent = await self._github()
        for target in targets:
            result = await agent.ainvoke({"messages": [{"role": "user", "content": f"""You are a read-only GitHub evidence collector. Access ONLY {target['owner']}/{target['repo']} on branch {target['branch']}.
Incident logs: {incident.logs or 'not supplied'}
Question: {focus}
Collect only relevant code-search results, up to three source files, and recent commits/PRs when relevant. Never modify anything. Return concise factual evidence with file paths and lines."""}]})
            reports.append({"repository": f"{target['owner']}/{target['repo']}", "evidence": str(result["messages"][-1].content)})
        return AgentFinding(agentName="GitHubEvidenceAgent", status="GITHUB_EVIDENCE_COMPLETED", summary=f"GitHub MCP evidence collected for: {focus or 'the active incident'}.", evidence=json.dumps(reports)), [item["repository"] for item in reports]

    async def propose_code_change(
        self,
        owner: str,
        incident: Incident,
        change_intent: str,
        findings: list[AgentFinding],
    ) -> str:
        """Use read-only GitHub MCP tools to locate and revise one owned source file."""
        agent = await self._github()
        evidence = "\n".join(
            f"- {finding.agent_name} [{finding.status}]: {finding.summary}\n  {finding.evidence}"
            for finding in findings
        ) or "No additional evidence was collected."
        result = await agent.ainvoke({"messages": [{"role": "user", "content": f"""You are generating a safe, single-file code proposal for an incident.

Use GitHub MCP tools only for repositories owned by `{owner}`. Discover the repository and source file from the incident and change intent; do not assume a repository name or path. Read the chosen file from branch `main` before proposing an edit. Never create, modify, merge, comment on, close, or delete anything.

Return `null` when you cannot establish one clearly relevant existing file on `main`, when the relevant repository is not owned by `{owner}`, or when the evidence does not justify a code change.

Otherwise return ONLY valid JSON with exactly these fields:
- repository: `owner/repository`
- filePath: a repository-relative path
- baseBranch: exactly `main`
- proposedCode: complete revised contents of the selected file, with no Markdown fence or prose

INCIDENT
ID: {incident.id}
Title: {incident.title}
Service: {incident.service}
Severity: {incident.severity}
Symptoms: {incident.symptoms}
Logs: {incident.logs or 'not supplied'}

CODE CHANGE INTENT
{change_intent}

EVIDENCE
{evidence}"""}]})
        return str(result["messages"][-1].content)

    async def database_evidence(self, incident: Incident, focus: str) -> tuple[AgentFinding, list[str]]:
        agent = await self._sql()
        response = await asyncio.to_thread(agent.invoke, {"input": f"""Collect read-only SQLite evidence for this incident.
Incident: {incident.title}; service={incident.service}; logs={incident.logs or 'not supplied'}
Question: {focus}
Inspect schema first. Query only tables and records relevant to this incident. Return factual records, counts, statuses, and timestamps."""})
        return AgentFinding(agentName="SqlEvidenceAgent", status="DATABASE_EVIDENCE_COMPLETED", summary=f"Read-only SQLite evidence collected for: {focus or 'the active incident'}.", evidence=str(response["output"])), [str(self._database_path())]

    async def summarize(self, question: str, findings: list[AgentFinding]) -> AgentFinding:
        evidence = "\n\n".join(f"{item.agent_name}: {item.evidence}" for item in findings)
        response = await _model().ainvoke(f"""You are a read-only evidence synthesizer. Summarize the GitHub and SQLite evidence below for the chat advisor. Separate confirmed facts from gaps. Do not propose unverified facts.\nQuestion: {question}\n\nEvidence:\n{evidence}""")
        return AgentFinding(agentName="EvidenceSummarizerAgent", status="EVIDENCE_SUMMARIZED", summary="GitHub and database evidence were combined for the chat advisor.", evidence=str(response.content))
