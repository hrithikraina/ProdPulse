"""Orchestrates similarity search, evidence collection, and recommendations."""

import logging
from agents.deployment_check import DeploymentCheckAgent
from agents.code_investigation import CodeInvestigationAgent
from domain.models import AgentFlowStep, Incident, IncidentAnalysis
from services.advisor import IncidentAdvisor
from services.confidence import assess_confidence
from vector.store import IncidentStore

logger = logging.getLogger(__name__)

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
        logger.info(f"Starting analysis for incident {incident.id} with similarity limit={limit}")
        
        logger.info(f"Searching similar historical incidents for {incident.id}")
        matches = await self._vector_store.search(incident, limit)
        logger.info(f"Found {len(matches)} similar incidents matching incident {incident.id}")
        
        findings = []
        # Retrieval is evidence, not a decision to skip operational checks.
        # A convincing old incident can still mask a fresh deployment regression.
        logger.info(f"Invoking DeploymentCheckAgent for incident {incident.id}")
        findings.append(self._deployment_agent.investigate(incident))
        
        if incident.logs:
            logger.info(f"Logs provided for incident {incident.id}. Invoking CodeInvestigationAgent.")
            findings.append(await self._code_agent.investigate(incident))
        else:
            logger.info(f"No logs provided for incident {incident.id}; skipping CodeInvestigationAgent.")
            
        logger.info(f"Generating recommendation assessment with advisor for incident {incident.id}")
        assessment = await self._advisor.recommend(incident, matches, findings)
        logger.info(f"Advisor recommendation successfully generated for incident {incident.id}")
        
        confidence = assess_confidence(incident, matches, findings, assessment)
        return IncidentAnalysis(
            incomingIncident=incident,
            similarIncidents=matches,
            agentFindings=findings,
            summary=assessment.summary,
            nextActionSteps=assessment.next_action_steps,
            rca=assessment.rca,
            codeChanges=assessment.code_changes,
            evidenceSummary=self._evidence_summary(matches, findings),
            confidence=confidence,
            agentFlow=[
                AgentFlowStep(agentName="AzureAISearchAgent", status="COMPLETED"),
                *[AgentFlowStep(agentName=finding.agent_name, status=finding.status) for finding in findings],
                AgentFlowStep(agentName="AzureOpenAIIncidentAdvisor", status="COMPLETED"),
            ],
        )

    @staticmethod
    def _evidence_summary(matches, findings) -> str:
        historical = ", ".join(
            f"{match.incident.id} ({match.similarity:.0%})" for match in matches
        ) or "No historical incident met the 85% similarity threshold"
        agent_evidence = "; ".join(
            f"{finding.agent_name} [{finding.status}]: {finding.summary}" for finding in findings
        ) or "No additional agent evidence"
        return f"Historical evidence: {historical}. Agent evidence: {agent_evidence}."
