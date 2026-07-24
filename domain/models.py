"""Request and response schemas used by the HTTP API and services."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

class Incident(ApiModel):
    id: str
    title: str
    service: str
    severity: str
    symptoms: str
    root_cause: str | None = Field(default=None, alias="rootCause")
    resolution: str | None = None
    logs: str | None = None

    def similarity_text(self) -> str:
        """Fields shared by open and resolved incidents for similarity search.

        Root cause and resolution are deliberately excluded: they are only known
        after an incident has been resolved and are evidence to return after a
        match, not attributes to use to find one.
        """
        return f"title: {self.title}; service: {self.service}; severity: {self.severity}; symptoms: {self.symptoms}"

class DeploymentRecord(ApiModel):
    deployment_id: str = Field(alias="deploymentId")
    service: str
    version: str
    deployed_at: datetime = Field(alias="deployedAt")
    deployed_by: str = Field(alias="deployedBy")
    change_summary: str = Field(alias="changeSummary")
    status: str

class SimilarIncident(ApiModel):
    incident: Incident
    similarity: float

class AgentFinding(ApiModel):
    agent_name: str = Field(alias="agentName")
    status: str
    summary: str
    evidence: str


class AgentFlowStep(ApiModel):
    agent_name: str = Field(alias="agentName")
    status: str


class ConfluenceSource(ApiModel):
    page_id: str = Field(alias="pageId")
    title: str
    url: str
    space_key: str = Field(alias="spaceKey")
    last_modified: datetime | None = Field(default=None, alias="lastModified")
    excerpt: str = Field(min_length=1, max_length=4000)
    issue_summary: str | None = Field(default=None, alias="issueSummary", max_length=1000)


class InitialAssessment(ApiModel):
    """Grounded, UI-ready result of the initial evidence analysis."""
    summary: str = Field(description="Short summary of all agent findings.")
    next_action_steps: list[str] = Field(alias="nextActionSteps", default_factory=list)
    rca: list[str] = Field(default_factory=list, max_length=10, description="Maximum ten concise RCA lines.")
    code_change_intent: str | None = Field(
        alias="codeChangeIntent",
        default=None,
        description="Internal, evidence-backed intent for a GitHub MCP code proposal; null when no code change is warranted.",
    )


class CodeChangeProposal(ApiModel):
    """A verified, single-file proposal returned by the initial analysis API."""

    repository: str = Field(min_length=3, max_length=200)
    file_path: str = Field(alias="filePath", min_length=1, max_length=500)
    base_branch: Literal["main"] = Field(alias="baseBranch")
    proposed_code: str = Field(alias="proposedCode", min_length=1)
    code_changes: str = Field(alias="codeChanges", min_length=1)


class ConfidenceScore(ApiModel):
    score: int = Field(ge=0, le=10)
    reason: str


class ConfidenceAssessment(ApiModel):
    rca: ConfidenceScore
    recommendation: ConfidenceScore


class IncidentAnalysis(ApiModel):
    analysis_id: str | None = Field(default=None, alias="analysisId")
    incoming_incident: Incident = Field(alias="incomingIncident")
    similar_incidents: list[SimilarIncident] = Field(alias="similarIncidents")
    agent_findings: list[AgentFinding] = Field(alias="agentFindings")
    confluence_sources: list[ConfluenceSource] = Field(default_factory=list, alias="confluenceSources")
    summary: str
    next_action_steps: list[str] = Field(alias="nextActionSteps")
    rca: list[str] = Field(max_length=10)
    code_changes: CodeChangeProposal | None = Field(alias="codeChanges")
    code_change_intent: str | None = Field(default=None, exclude=True, repr=False)
    evidence_summary: str = Field(alias="evidenceSummary")
    agent_flow: list[AgentFlowStep] = Field(alias="agentFlow")
    confidence: ConfidenceAssessment = Field(
        default_factory=lambda: ConfidenceAssessment(
            rca=ConfidenceScore(score=0, reason="No corroborating RCA evidence was collected."),
            recommendation=ConfidenceScore(score=0, reason="Recommendations require more supporting evidence."),
        )
    )

class AnalyzeRequest(ApiModel):
    incident: Incident
    limit: int = Field(default=3, ge=1, le=20)


class AnalysisChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=8000)


class DraftPrPreviewRequest(ApiModel):
    repository: str = Field(min_length=3, max_length=200)
    file_path: str = Field(alias="filePath", min_length=1, max_length=500)
    base_branch: str | None = Field(default=None, alias="baseBranch", max_length=250)


class DraftPrCreateRequest(ApiModel):
    preview_id: str = Field(alias="previewId", min_length=1, max_length=100)


class DraftPrPreviewResponse(ApiModel):
    preview_id: str = Field(alias="previewId")
    repository: str
    base_branch: str = Field(alias="baseBranch")
    file_path: str = Field(alias="filePath")
    patch: str


class DraftPrCreateResponse(ApiModel):
    url: str
    number: int
    branch: str


class ChatNarrative(ApiModel):
    answer: str
    agent_summary: str = Field(alias="agentSummary")
    code_changes: str | None = Field(default=None, alias="codeChanges")


class AnalysisChatResponse(ApiModel):
    answer: str
    agent_summary: str = Field(alias="agentSummary")
    evidence_summary: str = Field(alias="evidenceSummary")
    code_changes: str | None = Field(default=None, alias="codeChanges")
    agent_flow: list[AgentFlowStep] = Field(default_factory=list, alias="agentFlow")
    sources: list[str] = Field(default_factory=list)
    new_findings: list[AgentFinding] = Field(default_factory=list, alias="newFindings")
    agent_calls: list[str] = Field(default_factory=list, alias="agentCalls")

class HealthResponse(ApiModel):
    status: Literal["ok"]
    historical_incident_count: int = Field(alias="historicalIncidentCount")
