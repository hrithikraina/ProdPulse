"""Orchestrates similarity search, evidence collection, and recommendations."""

from agents.deployment_check import DeploymentCheckAgent
from agents.code_investigation import CodeInvestigationAgent
from domain.models import Incident, IncidentAnalysis
from services.advisor import IncidentAdvisor
from vector.store import IncidentStore

class IncidentManagementService:
    def __init__(
        self,
        vector_store: IncidentStore,
        advisor: IncidentAdvisor,
        deployment_agent: DeploymentCheckAgent,
        code_agent: CodeInvestigationAgent,
    ) -> None:
        self._vector_store = vector_store
        self._advisor = advisor
        self._deployment_agent = deployment_agent
        self._code_agent = code_agent
    async def analyze(self, incident: Incident, limit: int = 3) -> IncidentAnalysis:
        matches = await self._vector_store.search(incident, limit)
        findings = []
        # Retrieval is evidence, not a decision to skip operational checks.
        # A convincing old incident can still mask a fresh deployment regression.
        findings.append(self._deployment_agent.investigate(incident))
        if incident.logs:
            findings.append(await self._code_agent.investigate(incident))
        recommendation = await self._advisor.recommend(incident, matches, findings)
        return IncidentAnalysis(
            incomingIncident=incident,
            similarIncidents=matches,
            agentFindings=findings,
            recommendation=recommendation,
        )
