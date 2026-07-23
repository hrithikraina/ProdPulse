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


class InitialAssessment(ApiModel):
    """Grounded, UI-ready result of the initial evidence analysis."""
    summary: str = Field(description="Short summary of all agent findings.")
    next_action_steps: list[str] = Field(alias="nextActionSteps", default_factory=list)
    rca: list[str] = Field(default_factory=list, max_length=10, description="Maximum ten concise RCA lines.")
    code_changes: str | None = Field(alias="codeChanges", default=None, description="Actual proposed code only; null when no code is needed.")


class IncidentAnalysis(ApiModel):
    analysis_id: str | None = Field(default=None, alias="analysisId")
    incoming_incident: Incident = Field(alias="incomingIncident")
    similar_incidents: list[SimilarIncident] = Field(alias="similarIncidents")
    agent_findings: list[AgentFinding] = Field(alias="agentFindings")
    summary: str
    next_action_steps: list[str] = Field(alias="nextActionSteps")
    rca: list[str] = Field(max_length=10)
    code_changes: str | None = Field(alias="codeChanges")
    evidence_summary: str = Field(alias="evidenceSummary")
    agent_flow: list[AgentFlowStep] = Field(alias="agentFlow")

class AnalyzeRequest(ApiModel):
    incident: Incident
    limit: int = Field(default=3, ge=1, le=20)


class AnalysisChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=8000)


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
