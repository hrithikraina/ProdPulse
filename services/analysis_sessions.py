"""Temporary, in-process incident-analysis chat sessions and safe tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from agents.deployment_check import DeploymentCheckAgent
from domain.models import AgentFinding, AgentFlowStep, AnalysisChatResponse, ChatNarrative, Incident, IncidentAnalysis, SimilarIncident
from agents.chat_evidence_agents import ChatEvidenceAgents
from services.azure_openai import AzureOpenAIClient
from vector.store import IncidentStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AnalysisSession:
    incident: Incident
    historical_incidents: list[SimilarIncident]
    agent_findings: list[AgentFinding]
    initial_assessment: str
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=30))


class AnalysisSessionStore:
    """A TTL store intentionally scoped to one running API process."""

    def __init__(self, ttl: timedelta = timedelta(minutes=30)) -> None:
        self._ttl = ttl
        self._sessions: dict[str, AnalysisSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, analysis: IncidentAnalysis) -> str:
        session_id = f"analysis-{uuid4()}"
        logger.info(f"Creating new analysis session: {session_id} for incident: {analysis.incoming_incident.id}")
        async with self._lock:
            self._purge_expired()
            self._sessions[session_id] = AnalysisSession(
                incident=analysis.incoming_incident,
                historical_incidents=list(analysis.similar_incidents),
                agent_findings=list(analysis.agent_findings),
                initial_assessment=json.dumps({
                    "summary": analysis.summary,
                    "nextActionSteps": analysis.next_action_steps,
                    "rca": analysis.rca,
                    "codeChanges": analysis.code_changes,
                }),
                expires_at=datetime.now(UTC) + self._ttl,
            )
        logger.info(f"Session {session_id} successfully created.")
        return session_id

    async def get(self, session_id: str) -> AnalysisSession | None:
        async with self._lock:
            self._purge_expired()
            session = self._sessions.get(session_id)
            if session:
                logger.info(f"Session {session_id} retrieved successfully. Expires at: {session.expires_at}")
            else:
                logger.warning(f"Session {session_id} not found or has expired.")
            return session

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
            if removed:
                logger.info(f"Deleted session {session_id}")
            else:
                logger.warning(f"Failed to delete session {session_id}: session does not exist.")
            return removed

    async def touch(self, session: AnalysisSession) -> None:
        """Extend the inactivity timeout after a successful chat request."""
        async with self._lock:
            session.expires_at = datetime.now(UTC) + self._ttl
            logger.info(f"Session touch: extended expires_at to {session.expires_at}")

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        expired_keys = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for session_id in expired_keys:
            logger.info(f"Purging expired session: {session_id} (expired at {self._sessions[session_id].expires_at})")
            del self._sessions[session_id]


ToolHandler = Callable[[AnalysisSession, dict[str, Any]], Awaitable[tuple[AgentFinding, list[str]]]]


class IncidentToolRegistry:
    """The complete allow-list of evidence tools the model may request."""

    def __init__(
        self,
        vector_store: IncidentStore,
        deployment_agent: DeploymentCheckAgent,
        evidence_agents: ChatEvidenceAgents,
    ) -> None:
        self._vector_store = vector_store
        self._deployment_agent = deployment_agent
        self._evidence_agents = evidence_agents
        self._handlers: dict[str, ToolHandler] = {
            "search_historical_incidents": self._search_historical_incidents,
            "get_deployment_evidence": self._get_deployment_evidence,
            "get_github_evidence": self._get_github_evidence,
            "get_database_evidence": self._get_database_evidence,
            "run_deep_rca_evidence": self._run_deep_rca_evidence,
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [
            self._definition("search_historical_incidents", "Find similar resolved incidents, root causes, and prior resolutions when historical evidence is missing.", {"investigation_focus": {"type": "string", "description": "The aspect of the active incident to search for."}}),
            self._definition("get_deployment_evidence", "Retrieve approved read-only release, version, and change evidence for the active incident service. Use for timing or release-causality questions.", {}),
            self._definition("get_github_evidence", "Use for source-code, commits, pull requests, or repository questions. It uses the read-only GitHub MCP evidence agent.", {"focus": {"type": "string", "description": "Code or repository question to investigate."}}),
            self._definition("get_database_evidence", "Use for database, transaction state, table, record, count, status, or timestamp questions. It uses the read-only SQLite evidence agent.", {"focus": {"type": "string", "description": "Database question to investigate."}}),
            self._definition("run_deep_rca_evidence", "Run the existing read-only RCA workflow against supplied incident logs when deeper GitHub MCP or SQLite database evidence is needed. The workflow itself decides whether its configured GitHub and database evidence agents are relevant. Never use it when there are no logs.", {"focus": {"type": "string", "description": "The missing diagnostic evidence to clarify."}}),
        ]

    async def execute(self, name: str, session: AnalysisSession, arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        logger.info(f"Tool registry executing: {name} with arguments: {arguments}")
        handler = self._handlers.get(name)
        if not handler:
            logger.error(f"Tool execution failed: '{name}' is not in the approved registry.")
            raise ValueError(f"Tool '{name}' is not in the approved registry.")
        try:
            result = await handler(session, arguments)
            logger.info(f"Tool execution completed: {name}. Finding status: {result[0].status}")
            return result
        except Exception as error:
            logger.error(f"Error executing tool {name}: {error}", exc_info=True)
            raise

    async def summarize_evidence(self, question: str, findings: list[AgentFinding]) -> AgentFinding:
        """Use a dedicated LLM summarizer only when both chat specialists ran."""
        return await self._evidence_agents.summarize(question, findings)

    @staticmethod
    def _definition(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": [], "additionalProperties": False}}}

    async def _search_historical_incidents(self, session: AnalysisSession, arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        focus = str(arguments.get("investigation_focus", "")).strip()
        query = session.incident.model_copy(update={"symptoms": f"{session.incident.symptoms} {focus}".strip()})
        matches = await self._vector_store.search(query, 5)
        session.historical_incidents = self._merge_matches(session.historical_incidents, matches)
        evidence = "\n".join(f"{item.incident.id}: rootCause={item.incident.root_cause}; resolution={item.incident.resolution}" for item in matches) or "No additional resolved incidents found."
        return AgentFinding(agentName="AzureAISearchAgent", status="HISTORICAL_EVIDENCE_FOUND" if matches else "NO_HISTORICAL_EVIDENCE_FOUND", summary=f"Historical search completed for: {focus or 'active incident'}.", evidence=evidence), [item.incident.id for item in matches]

    async def _get_deployment_evidence(self, session: AnalysisSession, _arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        finding = self._deployment_agent.investigate(session.incident)
        return finding, _source_ids(finding.evidence)

    async def _get_github_evidence(self, session: AnalysisSession, arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        focus = str(arguments.get("focus", "")).strip()
        return await self._evidence_agents.github_evidence(session.incident, focus)

    async def _get_database_evidence(self, session: AnalysisSession, arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        focus = str(arguments.get("focus", "")).strip()
        return await self._evidence_agents.database_evidence(session.incident, focus)

    async def _run_deep_rca_evidence(self, session: AnalysisSession, arguments: dict[str, Any]) -> tuple[AgentFinding, list[str]]:
        """Reuse the existing log router, GitHub MCP, and read-only SQLite agents."""
        if not session.incident.logs:
            return AgentFinding(agentName="RcaGraphAgent", status="NO_LOGS_SUPPLIED", summary="Deep RCA evidence requires incident logs.", evidence="Provide application ERROR logs or a stack trace."), []
        try:
            # This module owns optional MCP/SQL dependencies, so import it only
            # when the model has requested this approved deep-investigation tool.
            from agents.rca_graph import run_rca
            combined_context = f"Service: {session.incident.service}\nSymptoms: {session.incident.symptoms}\nLogs: {session.incident.logs}"
            result = await run_rca(combined_context)
        except Exception as error:
            return AgentFinding(agentName="RcaGraphAgent", status="RCA_EVIDENCE_UNAVAILABLE", summary="The deep RCA workflow could not collect additional evidence.", evidence=str(error)), []
        github = result.get("github_evidence_round_2") or result.get("github_evidence_round_1") or []
        database = result.get("database_evidence_round_2") or result.get("database_evidence_round_1") or []
        evidence = json.dumps({"focus": str(arguments.get("focus", "")), "githubMcpEvidence": github, "databaseEvidence": database, "summary": result.get("summary", "")}, default=str)[:16000]
        sources = (["GitHub MCP"] if github else []) + (["SQLite read-only RCA"] if database else [])
        return AgentFinding(agentName="RcaGraphAgent", status="RCA_EVIDENCE_COMPLETED", summary="The existing RCA workflow completed; its log router selected only the relevant configured evidence agents.", evidence=evidence), sources




    @staticmethod
    def _merge_matches(existing: list[SimilarIncident], additional: list[SimilarIncident]) -> list[SimilarIncident]:
        by_id = {match.incident.id: match for match in existing}
        by_id.update({match.incident.id: match for match in additional})
        return list(by_id.values())


class AnalysisChatService:
    def __init__(self, sessions: AnalysisSessionStore, registry: IncidentToolRegistry, client: AzureOpenAIClient) -> None:
        self._sessions = sessions
        self._registry = registry
        self._client = client

    async def chat(self, analysis_id: str, message: str) -> AnalysisChatResponse | None:
        logger.info(f"Starting chat turn for session {analysis_id}")
        session = await self._sessions.get(analysis_id)
        if session is None:
            logger.warning(f"Chat turn failed: session {analysis_id} not found.")
            return None
        messages = [self._system_message(session), *session.recent_messages, {"role": "user", "content": message}]
        calls: list[str] = []
        new_findings: list[AgentFinding] = []
        flow: list[AgentFlowStep] = []
        sources = self._session_sources(session)
        for loop_idx in range(3):
            logger.info(f"Submitting chat messages to Azure OpenAI (turn loop {loop_idx + 1}/3)")
            response = await self._client.chat_with_tools(messages, self._registry.definitions())
            assistant_message = response["message"]
            messages.append(assistant_message)
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                logger.info("Azure OpenAI responded without requesting any tool calls. Finalizing narrative response.")
                narrative = self._narrative(
                    str(assistant_message.get("content") or "Evidence is insufficient to answer that safely.").strip(),
                    new_findings,
                )
                session.recent_messages = (messages[1:])[-12:]
                await self._sessions.touch(session)
                flow.append(AgentFlowStep(agentName="ProdPlusIncidentAdvisor", status="COMPLETED"))
                logger.info(f"Chat turn completed successfully for session {analysis_id}.")
                return AnalysisChatResponse(answer=narrative.answer, agentSummary=narrative.agent_summary, evidenceSummary=self._evidence_summary(session), codeChanges=narrative.code_changes, agentFlow=flow, sources=list(dict.fromkeys(sources)), newFindings=new_findings, agentCalls=calls)
            
            logger.info(f"Azure OpenAI requested {len(tool_calls)} tool call(s). Running tools.")
            round_findings: list[AgentFinding] = []
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object.")
                    finding, finding_sources = await self._registry.execute(name, session, arguments)
                    calls.append(name)
                    new_findings.append(finding)
                    round_findings.append(finding)
                    session.agent_findings.append(finding)
                    sources.extend(finding_sources)
                    flow.append(AgentFlowStep(agentName=finding.agent_name, status=finding.status))
                    content = finding.model_dump_json(by_alias=True)
                except (ValueError, json.JSONDecodeError, RuntimeError) as error:
                    logger.error(f"Error handling tool call {name} in chat loop: {error}", exc_info=True)
                    content = json.dumps({"error": str(error)})
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": content})
            evidence_specialists = [finding for finding in round_findings if finding.agent_name in {"GitHubEvidenceAgent", "SqlEvidenceAgent"}]
            if len(evidence_specialists) > 1:
                synthesis = await self._registry.summarize_evidence(message, evidence_specialists)
                new_findings.append(synthesis)
                session.agent_findings.append(synthesis)
                flow.append(AgentFlowStep(agentName=synthesis.agent_name, status=synthesis.status))
                messages.append({"role": "user", "content": "Approved combined GitHub and database evidence for this question:\n" + synthesis.evidence})
        session.recent_messages = (messages[1:])[-12:]
        await self._sessions.touch(session)
        flow.append(AgentFlowStep(agentName="ProdPlusIncidentAdvisor", status="INCOMPLETE"))
        return AnalysisChatResponse(answer="I gathered the available approved evidence but could not complete a safe answer. Please refine the question.", agentSummary=self._finding_summary(new_findings), evidenceSummary=self._evidence_summary(session), codeChanges=None, agentFlow=flow, sources=list(dict.fromkeys(sources)), newFindings=new_findings, agentCalls=calls)

    @staticmethod
    def _system_message(session: AnalysisSession) -> dict[str, str]:
        history = "\n".join(f"- {item.incident.id}: rootCause={item.incident.root_cause}; resolution={item.incident.resolution}" for item in session.historical_incidents) or "None"
        findings = "\n".join(f"- {item.agent_name}: {item.summary} Evidence: {item.evidence}" for item in session.agent_findings) or "None"
        evidence = f"Incoming incident: {session.incident.similarity_text()}\nInitial structured assessment: {session.initial_assessment}\nHistorical evidence:\n{history}\nAgent findings:\n{findings}"
        return {"role": "system", "content": "You are Prod+ Incident Advisor. Use only evidence in this message. Answer directly when it is enough; otherwise request only an approved tool needed to obtain missing evidence. Never claim access to production, execute changes, deploy, rollback, modify source, change balances, or contact counterparties. Clearly distinguish evidence from hypotheses and cite evidence source IDs/paths in your answer. For your final answer, return ONLY valid JSON with answer, agentSummary (one short combined summary of the evidence agents used in this turn), and codeChanges (a complete proposed code snippet only, without Markdown fences, or null).\n\nCURRENT SESSION EVIDENCE:\n" + evidence}

    @staticmethod
    def _session_sources(session: AnalysisSession) -> list[str]:
        sources = [item.incident.id for item in session.historical_incidents]
        for finding in session.agent_findings:
            sources.extend(_source_ids(finding.evidence))
        return sources

    @classmethod
    def _narrative(cls, content: str, findings: list[AgentFinding]) -> ChatNarrative:
        try:
            return ChatNarrative.model_validate(json.loads(cls._json_content(content)))
        except (json.JSONDecodeError, ValueError):
            # Keep the endpoint structured even if a model ignores the JSON instruction.
            return ChatNarrative(answer=content, agentSummary=cls._finding_summary(findings), codeChanges=None)

    @staticmethod
    def _finding_summary(findings: list[AgentFinding]) -> str:
        return "; ".join(f"{finding.agent_name}: {finding.summary}" for finding in findings) or "No additional agent was required; the answer uses existing session evidence."

    @staticmethod
    def _evidence_summary(session: AnalysisSession) -> str:
        historical = ", ".join(
            f"{match.incident.id} ({match.similarity:.0%})" for match in session.historical_incidents
        ) or "No historical incident met the 85% similarity threshold"
        findings = "; ".join(
            f"{finding.agent_name} [{finding.status}]: {finding.summary}" for finding in session.agent_findings
        ) or "No agent evidence"
        return f"Historical evidence: {historical}. Agent evidence: {findings}."

    @staticmethod
    def _json_content(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            return stripped.split("\n", 1)[1].rsplit("\n", 1)[0]
        return stripped


def _source_ids(evidence: str) -> list[str]:
    return re.findall(r"(?:INC|DEP)-[A-Za-z0-9-]+", evidence)
