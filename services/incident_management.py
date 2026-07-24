"""Orchestrates similarity search, evidence collection, and recommendations."""

import logging
from agents.deployment_check import DeploymentCheckAgent
from agents.code_investigation import CodeInvestigationAgent
from domain.models import AgentFinding, AgentFlowStep, ConfluenceSource, Incident, IncidentAnalysis
from repositories.confluence_repository import ConfluenceRepository, ConfluenceRepositoryError
from services.advisor import IncidentAdvisor
from services.confluence_summarizer import ConfluenceSummarizer
from services.confidence import assess_confidence
from services.code_change_proposal import CodeChangeProposalService
from vector.store import IncidentStore

logger = logging.getLogger(__name__)

class IncidentManagementService:
    def __init__(
        self,
        vector_store: IncidentStore,
        advisor: IncidentAdvisor,
        deployment_agent: DeploymentCheckAgent,
        code_agent: CodeInvestigationAgent,
        confluence_repository: ConfluenceRepository | None = None,
        confluence_summarizer: ConfluenceSummarizer | None = None,
        code_change_proposal: CodeChangeProposalService | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._advisor = advisor
        self._deployment_agent = deployment_agent
        self._code_agent = code_agent
        self._confluence_repository = confluence_repository
        self._confluence_summarizer = confluence_summarizer
        self._code_change_proposal = code_change_proposal
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

        confluence_finding, confluence_sources = await self._confluence_evidence(incident)
        findings.append(confluence_finding)

        logger.info(f"Generating recommendation assessment with advisor for incident {incident.id}")
        assessment = await self._advisor.recommend(incident, matches, findings)
        logger.info(f"Advisor recommendation successfully generated for incident {incident.id}")
        
        confidence = assess_confidence(
            incident,
            matches,
            findings,
            assessment,
            confluence_sources=confluence_sources,
        )
        code_change_proposal = (
            await self._code_change_proposal.propose(incident, assessment.code_change_intent, findings)
            if self._code_change_proposal is not None
            else None
        )
        return IncidentAnalysis(
            incomingIncident=incident,
            similarIncidents=matches,
            agentFindings=findings,
            confluenceSources=confluence_sources,
            summary=assessment.summary,
            nextActionSteps=assessment.next_action_steps,
            rca=assessment.rca,
            codeChanges=code_change_proposal,
            code_change_intent=assessment.code_change_intent,
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

    async def _confluence_evidence(
        self, incident: Incident
    ) -> tuple[AgentFinding, list[ConfluenceSource]]:
        if self._confluence_repository is None:
            return (
                AgentFinding(
                    agentName="ConfluenceKnowledgeAgent",
                    status="CONFLUENCE_NOT_CONFIGURED",
                    summary="Confluence evidence retrieval is not configured.",
                    evidence="No approved Confluence configuration is available.",
                ),
                [],
            )

        try:
            sources = await self._confluence_repository.search(incident)
        except ConfluenceRepositoryError as error:
            logger.warning(
                "Confluence evidence was unavailable for incident %s: %s",
                incident.id,
                type(error).__name__,
            )
            return (
                AgentFinding(
                    agentName="ConfluenceKnowledgeAgent",
                    status="CONFLUENCE_BACKEND_UNAVAILABLE",
                    summary="Confluence evidence could not be retrieved; analysis continued with other evidence.",
                    evidence=str(error)[:500],
                ),
                [],
            )
        except Exception as error:
            logger.warning(
                "Unexpected Confluence failure for incident %s: %s",
                incident.id,
                type(error).__name__,
            )
            return (
                AgentFinding(
                    agentName="ConfluenceKnowledgeAgent",
                    status="CONFLUENCE_BACKEND_UNAVAILABLE",
                    summary="Confluence evidence could not be retrieved; analysis continued with other evidence.",
                    evidence="Confluence evidence retrieval encountered an unexpected failure.",
                ),
                [],
            )

        if not sources:
            return (
                AgentFinding(
                    agentName="ConfluenceKnowledgeAgent",
                    status="NO_CONFLUENCE_EVIDENCE_FOUND",
                    summary="No relevant pages were found in the approved Confluence spaces.",
                    evidence="The allow-listed Confluence page search completed without usable evidence.",
                ),
                [],
            )

        summary_failure = False
        if self._confluence_summarizer is not None:
            try:
                sources = await self._confluence_summarizer.summarize(incident, sources)
            except Exception as error:
                summary_failure = True
                logger.warning(
                    "Confluence issue summarization was unavailable for incident %s: %s",
                    incident.id,
                    type(error).__name__,
                )
        else:
            summary_failure = True

        summarized_count = sum(source.issue_summary is not None for source in sources)
        evidence = "\n\n".join(
            (
                f"pageId={source.page_id}, title={source.title}, url={source.url}, "
                f"spaceKey={source.space_key}, "
                + (
                    f"issueSummary={source.issue_summary}"
                    if source.issue_summary
                    else f"excerptFallback={source.excerpt}"
                )
            )
            for source in sources
        )
        if summary_failure or summarized_count < len(sources):
            summary_detail = (
                f"Issue-focused summaries were created for {summarized_count} of "
                f"{len(sources)} page(s); bounded excerpts were used for the remainder."
            )
        else:
            summary_detail = "Issue-focused summaries were created for all retrieved pages."
        return (
            AgentFinding(
                agentName="ConfluenceKnowledgeAgent",
                status="CONFLUENCE_EVIDENCE_FOUND",
                summary=(
                    f"Found {len(sources)} relevant page(s) in approved Confluence spaces. "
                    f"{summary_detail}"
                ),
                evidence=evidence,
            ),
            sources,
        )
